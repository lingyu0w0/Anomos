#!/usr/bin/env python

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Original version written by Henry 'Pi' James, modified by (at least)
# John Hoffman and Uoti Urpala

from __future__ import division

SPEW_SCROLL_RATE = 1

import sys
import os
import threading
from time import time, strftime

from Anomos.download import Feedback, Multitorrent
from Anomos.defaultargs import get_defaults
from Anomos.parseargs import parseargs, printHelp
from Anomos.zurllib import urlopen
from Anomos.bencode import bdecode
from Anomos.ConvertedMetainfo import ConvertedMetainfo
from Anomos import configfile
from Anomos import BTFailure
from Anomos import version
from Anomos import ADD_TASK


try:
    import curses
    import curses.panel
    from curses.wrapper import wrapper as curses_wrapper
    from signal import signal, SIGWINCH
except:
    print 'Textmode GUI initialization failed, cannot proceed.'
    print
    print 'This download interface requires the standard Python module ' \
       '"curses", which is unfortunately not available for the native ' \
       'Windows port of Python. It is however available for the Cygwin ' \
       'port of Python, running on all Win32 systems (www.cygwin.com).'
    print
    print 'You may still use "anondownloadheadless.py" to download.'
    sys.exit(1)

def fmttime(n):
    if n == 0:
        return 'download complete!'
    try:
        n = int(n)
        assert n >= 0 and n < 5184000  # 60 days
    except:
        return '<unknown>'
    m, s = divmod(n, 60)
    h, m = divmod(m, 60)
    return 'finishing in %d:%02d:%02d' % (h, m, s)

def fmtsize(n):
    s = str(n)
    size = s[-3:]
    while len(s) > 3:
        s = s[:-3]
        size = '%s,%s' % (s[-3:], size)
    if n > 999:
        unit = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB', 'YiB']
        i = 1
        while i + 1 < len(unit) and (n >> 10) >= 999:
            i += 1
            n >>= 10
        n /= (1 << 10)
        size = '%s (%.0f %s)' % (size, n, unit[i])
    return size


class CursesDisplayer(object):

    def __init__(self, scrwin, errlist, doneflag, reread_config):
        self.scrwin = scrwin
        self.errlist = errlist
        self.doneflag = doneflag

        signal(SIGWINCH, self.winch_handler)
        self.changeflag = threading.Event()

        self.done = False
        self.reread_config = reread_config
        self.activity = ''
        self.status = ''
        self.progress = ''
        self.downRate = '---'
        self.upRate = '---'
        self.relayRate = '---'
        self.relayCount = 0
        self.shareRating = ''
        self.seedStatus = ''
        self.peerStatus = ''
        self.errors = []
        self.file = ''
        self.downloadTo = ''
        self.fileSize = ''
        self.numpieces = 0
        self.spew_scroll_time = 0
        self.spew_scroll_pos = 0

        self._remake_window()

    def set_torrent_values(self, name, path, size, numpieces):
        self.file = name
        self.downloadTo = path
        self.fileSize = fmtsize(size)
        self.numpieces = numpieces
        self._remake_window()

    def winch_handler(self, signum, stackframe):
        self.changeflag.set()
        curses.endwin()
        self.scrwin.refresh()
        self.scrwin = curses.newwin(0, 0, 0, 0)
        self._remake_window()

    def _remake_window(self):
        self.scrh, self.scrw = self.scrwin.getmaxyx()
        self.scrpan = curses.panel.new_panel(self.scrwin)
        self.labelh, self.labelw, self.labely, self.labelx = 11, 9, 1, 2
        self.labelwin = curses.newwin(self.labelh, self.labelw,
                                      self.labely, self.labelx)
        self.labelpan = curses.panel.new_panel(self.labelwin)
        self.fieldh, self.fieldw, self.fieldy, self.fieldx = (
                            self.labelh, self.scrw-2 - self.labelw-3,
                            1, self.labelw+3)
        self.fieldwin = curses.newwin(self.fieldh, self.fieldw,
                                      self.fieldy, self.fieldx)
        self.fieldwin.nodelay(1)
        self.fieldpan = curses.panel.new_panel(self.fieldwin)
        self.spewh, self.speww, self.spewy, self.spewx = (
            self.scrh - self.labelh - 2, self.scrw - 3, 1 + self.labelh, 2)
        self.spewwin = curses.newwin(self.spewh, self.speww,
                                     self.spewy, self.spewx)
        self.spewpan = curses.panel.new_panel(self.spewwin)
        try:
            self.scrwin.border(ord('|'),ord('|'),ord('-'),ord('-'),ord(' '),ord(' '),ord(' '),ord(' '))
        except:
            pass
        self.labelwin.addstr(0, 0, 'file:')
        self.labelwin.addstr(1, 0, 'size:')
        self.labelwin.addstr(2, 0, 'dest:')
        self.labelwin.addstr(3, 0, 'progress:')
        self.labelwin.addstr(4, 0, 'status:')
        self.labelwin.addstr(5, 0, 'dl rate:')
        self.labelwin.addstr(6, 0, 'ul rate:')
        self.labelwin.addstr(7, 0, 're rate:')
        self.labelwin.addstr(8, 0, 'sharing:')
        self.labelwin.addstr(9, 0, 'seeds:')
        self.labelwin.addstr(10, 0, 'peers:')
        curses.panel.update_panels()
        curses.doupdate()
        self.changeflag.clear()


    def finished(self):
        self.done = True
        self.downRate = '---'
        self.display({'activity':'download succeeded', 'fractionDone':1})

    def error(self, errormsg):
        newerrmsg = strftime('[%H:%M:%S] ') + str(errormsg)
        self.errors.append(newerrmsg.split('\n')[0])
        self.errlist.append(newerrmsg)
        self.display({})

    def display(self, statistics):
        fractionDone = statistics.get('fractionDone')
        activity = statistics.get('activity')
        timeEst = statistics.get('timeEst')
        downRate = statistics.get('downRate')
        upRate = statistics.get('upRate')
        relayRate = statistics.get('relayRate')
        relayCount = statistics.get('relayCount')
        spew = statistics.get('spew')

        inchar = self.fieldwin.getch()
        if inchar == 12: # ^L
            self._remake_window()
        elif inchar in (ord('q'),ord('Q')):
            self.doneflag.set()
        elif inchar in (ord('r'),ord('R')):
            self.reread_config()

        if timeEst is not None:
            self.activity = fmttime(timeEst)
        elif activity is not None:
            self.activity = activity
        if self.changeflag.isSet():
            return

        if fractionDone is not None:
            blocknum = int(self.fieldw * fractionDone)
            self.progress = blocknum * '#' + (self.fieldw - blocknum) * '_'
            self.status = '%s (%.1f%%)' % (self.activity, fractionDone * 100)

        if downRate is not None:
            self.downRate = '%.1f KB/s' % (downRate / (1 << 10))
        if upRate is not None:
            self.upRate = '%.1f KB/s' % (upRate / (1 << 10))
        if relayCount is not None:
            self.relayCount = relayCount
        if relayRate is not None:
            self.relayRate = '%.1f KB/s (%d)' % (relayRate / (1 << 10), self.relayCount)
        downTotal = statistics.get('downTotal')
        if downTotal is not None:
            upTotal = statistics['upTotal']
            if downTotal <= upTotal / 100:
                self.shareRating = 'oo  (%.1f MB up / %.1f MB down)' % (
                    upTotal / (1<<20), downTotal / (1<<20))
            else:
                self.shareRating = '%.3f  (%.1f MB up / %.1f MB down)' % (
                   upTotal / downTotal, upTotal / (1<<20), downTotal / (1<<20))
            numCopies = statistics['numCopies']
            nextCopies = ', '.join(["%d:%.1f%%" % (a,int(b*1000)/10) for a,b in
                    zip(xrange(numCopies+1, 1000), statistics['numCopyList'])])
            if not self.done:
                self.seedStatus = '%d seen now, plus %d distributed copies ' \
                                  '(%s)' % (statistics['numSeeds'],
                                         statistics['numCopies'], nextCopies)
            else:
                self.seedStatus = '%d distributed copies (next: %s)' % (
                    statistics['numCopies'], nextCopies)
            self.peerStatus = '%d seen now' % statistics['numPeers']

        self.fieldwin.erase()
        self.fieldwin.addnstr(0, 0, self.file, self.fieldw, curses.A_BOLD)
        self.fieldwin.addnstr(1, 0, self.fileSize, self.fieldw)
        self.fieldwin.addnstr(2, 0, self.downloadTo, self.fieldw)
        if self.progress:
            self.fieldwin.addnstr(3, 0, self.progress, self.fieldw, curses.A_BOLD)
        self.fieldwin.addnstr(4, 0, self.status, self.fieldw)
        self.fieldwin.addnstr(5, 0, self.downRate, self.fieldw)
        self.fieldwin.addnstr(6, 0, self.upRate, self.fieldw)
        self.fieldwin.addnstr(7, 0, self.relayRate, self.fieldw)
        self.fieldwin.addnstr(8, 0, self.shareRating, self.fieldw)
        self.fieldwin.addnstr(9, 0, self.seedStatus, self.fieldw)
        self.fieldwin.addnstr(10, 0, self.peerStatus, self.fieldw)

        self.spewwin.erase()

        if not spew:
            errsize = self.spewh
            if self.errors:
                self.spewwin.addnstr(0, 0, "Log:", self.speww, curses.A_BOLD)
                errsize = len(self.errors)
                displaysize = min(errsize, self.spewh)
                displaytop = errsize - displaysize
                for i in range(displaysize):
                    self.spewwin.addnstr(i, self.labelw, self.errors[displaytop + i],
                                 self.speww-self.labelw-1, curses.A_BOLD)
        else:
            if self.errors:
                self.spewwin.addnstr(0, 0, "error:", self.speww, curses.A_BOLD)
                self.spewwin.addnstr(0, self.labelw, self.errors[-1],
                                 self.speww-self.labelw-1, curses.A_BOLD)
            self.spewwin.addnstr(2, 0, "  #     IP                 Upload           Download     Completed  Speed", self.speww, curses.A_BOLD)


            if self.spew_scroll_time + SPEW_SCROLL_RATE < time():
                self.spew_scroll_time = time()
                if len(spew) > self.spewh-5 or self.spew_scroll_pos > 0:
                    self.spew_scroll_pos += 1
            if self.spew_scroll_pos > len(spew):
                self.spew_scroll_pos = 0

            for i in range(len(spew)):
                spew[i]['lineno'] = i+1
            spew.append({'lineno': None})
            spew = spew[self.spew_scroll_pos:] + spew[:self.spew_scroll_pos]

            for i in range(min(self.spewh - 5, len(spew))):
                if not spew[i]['lineno']:
                    continue
                self.spewwin.addnstr(i+3, 0, '%3d' % spew[i]['lineno'], 3)
                self.spewwin.addnstr(i+3, 4, spew[i]['ip'], 15)
                ul = spew[i]['upload']
                if ul[1] > 100:
                    self.spewwin.addnstr(i+3, 20, '%6.0f KB/s' % (
                        ul[1] / 1000), 11)
                self.spewwin.addnstr(i+3, 32, '-----', 5)
                if ul[2]:
                    self.spewwin.addnstr(i+3, 33, 'I', 1)
                if ul[3]:
                    self.spewwin.addnstr(i+3, 35, 'C', 1)
                dl = spew[i]['download']
                if dl[1] > 100:
                    self.spewwin.addnstr(i+3, 38, '%6.0f KB/s' % (
                        dl[1] / 1000), 11)
                self.spewwin.addnstr(i+3, 50, '-------', 7)
                if dl[2]:
                    self.spewwin.addnstr(i+3, 51, 'I', 1)
                if dl[3]:
                    self.spewwin.addnstr(i+3, 53, 'C', 1)
                if dl[4]:
                    self.spewwin.addnstr(i+3, 55, 'S', 1)
                self.spewwin.addnstr(i+3, 58, '%5.1f%%' % (int(spew[i]['completed']*1000)/10), 6)
                if spew[i]['speed'] is not None:
                    self.spewwin.addnstr(i+3, 64, '%5.0f KB/s' % (spew[i]['speed']/1000), 10)

            self.spewwin.addnstr(self.spewh-1, 0,
                    "downloading %d pieces, have %d fragments, "
                    "%d of %d pieces completed" %
                    (statistics['storage_active'], statistics['storage_dirty'],
                     statistics['storage_numcomplete'], self.numpieces),
                    self.speww-1)

        curses.panel.update_panels()
        curses.doupdate()


class DL(Feedback):

    def __init__(self, metainfo, config, errlist):
        self.doneflag = threading.Event()
        self.metainfo = metainfo
        self.config = config
        self.errlist = errlist

    def run(self, scrwin):
        def reread():
            ADD_TASK(0, self.reread_config)
        self.d = CursesDisplayer(scrwin, self.errlist, self.doneflag, reread)
        try:
            self.multitorrent = Multitorrent(self.config, self.doneflag)
            # raises BTFailure if bad
            metainfo = ConvertedMetainfo(bdecode(self.metainfo))
            torrent_name = metainfo.name_fs
            if config['save_as']:
                if config['save_in']:
                    raise BTFailure('You cannot specify both --save_as and '
                                    '--save_in')
                saveas = config['save_as']
            elif config['save_in']:
                saveas = os.path.join(config['save_in'], torrent_name)
            else:
                saveas = torrent_name

            self.d.set_torrent_values(metainfo.name, os.path.abspath(saveas),
                                metainfo.file_size, len(metainfo.hashes))
            self.torrent = self.multitorrent.start_torrent(metainfo,
                                self.config, self, saveas)
        except BTFailure, e:
            errlist.append(str(e))
            return
        self.get_status()
        self.multitorrent.rawserver.listen_forever()
        self.d.display({'activity':'shutting down', 'fractionDone':0})
        self.torrent.shutdown()

    def reread_config(self):
        try:
            newvalues = configfile.get_config(self.config, 'anondownloadcurses')
        except Exception, e:
            self.d.error('Error reading config: ' + str(e))
            return
        self.config.update(newvalues)
        # The set_option call can potentially trigger something that kills
        # the torrent (when writing this the only possibility is a change in
        # max_files_open causing an IOError while closing files), and so
        # the self.failed() callback can run during this loop.
        for option, value in newvalues.iteritems():
            self.multitorrent.set_option(option, value)
        for option, value in newvalues.iteritems():
            self.torrent.set_option(option, value)

    def get_status(self):
        ADD_TASK(self.config['display_interval'], self.get_status)
        status = self.torrent.get_status(self.config['spew'])
        self.d.display(status)

    #def global_error(self, level, text):
    #    self.d.error(text)

    #def error(self, torrent, level, text):
    #    self.d.error(text)

    def failed(self, torrent, is_external):
        self.doneflag.set()

    def finished(self, torrent):
        self.d.finished()


if __name__ == '__main__':
    uiname = 'anondownloadcurses'
    defaults = get_defaults(uiname)

    if len(sys.argv) <= 1:
        printHelp(uiname, defaults)
        sys.exit(1)
    try:
        config, args = configfile.parse_configuration_and_args(defaults,
                                       uiname, sys.argv[1:], 0, 1)
        if args:
            if config['responsefile']:
                raise BTFailure, 'must have responsefile as arg or ' \
                      'parameter, not both'
            config['responsefile'] = args[0]
        try:
            if config['responsefile']:
                h = file(config['responsefile'], 'rb')
                metainfo = h.read()
                h.close()
            elif config['url']:
                h = urlopen(config['url'])
                metainfo = h.read()
                h.close()
            else:
                raise BTFailure('you need to specify a .torrent file')
        except IOError, e:
            raise BTFailure('Error reading .torrent file: ', str(e))
    except BTFailure, e:
        print str(e)
        sys.exit(1)

    errlist = []
    dl = DL(metainfo, config, errlist)
    curses_wrapper(dl.run)

    if errlist:
       print "These messages were logged during execution:"
       for error in errlist:
          print error

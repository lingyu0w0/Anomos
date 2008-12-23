# The contents of this file are subject to the BitTorrent Open Source License
# Version 1.0 (the License).  You may not copy or use this file, in either
# source code or executable form, except in compliance with the License.  You
# may obtain a copy of the License at http://www.bittorrent.com/license/.
#
# Software distributed under the License is distributed on an AS IS basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.  See the License
# for the specific language governing rights and limitations under the
# License.

# Written by Bram Cohen and John Hoffman

import sys
import os
import signal
import re
from threading import Event
from urlparse import urlparse
from traceback import print_exc
from time import gmtime, strftime
from random import shuffle, sample
from types import StringType, IntType, LongType, ListType, DictType
from binascii import b2a_hex, a2b_hex
from cStringIO import StringIO
from base64 import urlsafe_b64decode

from Anomos.obsoletepythonsupport import *

from Anomos.parseargs import parseargs, formatDefinitions
from Anomos.RawServer import RawServer
from Anomos.HTTPHandler import HTTPHandler
from Anomos.parsedir import parsedir
from Anomos.platform import bttime
from Anomos.NatCheck import NatCheck
from Anomos.bencode import bencode, bdecode, Bencached
from Anomos.zurllib import quote, unquote
from Anomos import version

from Anomos.crypto import Certificate, AESKeyManager, AESKey, initCrypto, CryptoError
from Anomos.NetworkModel import NetworkModel

defaults = [
    ('port', 80, "Port to listen on."),
    ('dfile', None, 'file to store recent downloader info in'),
    ('bind', '', 'ip to bind to locally'),
    ('socket_timeout', 15, 'timeout for closing connections'),
    ('save_dfile_interval', 5 * 60, 'seconds between saving dfile'),
    ('timeout_downloaders_interval', 45 * 60, 'seconds between expiring downloaders'),
    ('reannounce_interval', 30 * 60, 'seconds downloaders should wait between reannouncements'),
    ('response_size', 50, 'default number of peers to send in an info message if the client does not specify a number'),
    ('timeout_check_interval', 5,
        'time to wait between checking if any connections have timed out'),
    ('nat_check', 3,
        "how many times to check if a downloader is behind a NAT (0 = don't check)"),
    ('log_nat_checks', 0,
        "whether to add entries to the log for nat-check results"),
    ('min_time_between_log_flushes', 3.0,
        'minimum time it must have been since the last flush to do another one'),
    ('min_time_between_cache_refreshes', 600.0,
        'minimum time in seconds before a cache is considered stale and is flushed'),
    ('allowed_dir', os.getcwd(), 'only allow downloads for .torrents in this dir (and recursively in subdirectories of directories that have no .torrent files themselves). If set, torrents in this directory show up on infopage/scrape whether they have peers or not'),
    ('parse_dir_interval', 60, 'how often to rescan the torrent directory, in seconds'),
    ('allowed_controls', 0, 'allow special keys in torrents in the allowed_dir to affect tracker access'),
    ('hupmonitor', 0, 'whether to reopen the log file upon receipt of HUP signal'),
    ('show_infopage', 1, "whether to display an info page when the tracker's root dir is loaded"),
    ('infopage_redirect', '', 'a URL to redirect the info page to'),
    ('show_names', 1, 'whether to display names from allowed dir'),
    ('favicon', '', 'file containing x-icon data to return when browser requests favicon.ico'),
    ('only_local_override_ip', 2, "ignore the ip GET parameter from machines which aren't on local network IPs (0 = never, 1 = always, 2 = ignore if NAT checking is not enabled). HTTP proxy headers giving address of original client are treated the same as --ip."),
    ('logfile', '', 'file to write the tracker logs, use - for stdout (default)'),
    ('allow_get', 0, 'use with allowed_dir; adds a /file?hash={hash} url that allows users to download the torrent file'),
    ('keep_dead', 0, 'keep dead torrents after they expire (so they still show up on your /scrape and web page). Only matters if allowed_dir is not set'),
    ('scrape_allowed', 'full', 'scrape access allowed (can be none, specific or full)'),
    ('max_give', 200, 'maximum number of peers to give with any one request'),
    ('data_dir', '', 'Directory in which to store cryptographic keys'),
    ]

def statefiletemplate(x):
    if type(x) != DictType:
        raise ValueError
    for cname, cinfo in x.items():
        if cname == 'peers':
            for y in cinfo.values():      # The 'peers' key is a dictionary of SHA hashes (torrent ids)
                 if type(y) != DictType:   # ... for the active torrents, and each is a dictionary
                     raise ValueError
                 for peerid, info in y.items(): # ... of client ids interested in that torrent
                     if (len(peerid) != 20):
                         raise ValueError
                     if type(info) != DictType:  # ... each of which is also a dictionary
                         raise ValueError # ... which has an IP, a Port, and a Bytes Left count for that client for that torrent
                     if type(info.get('ip', '')) != StringType:
                         raise ValueError
                     port = info.get('port')
                     if type(port) not in (IntType, LongType) or port < 0:
                         raise ValueError
                     left = info.get('left')
                     if type(left) not in (IntType, LongType) or left < 0:
                         raise ValueError
        elif cname == 'completed':
            if (type(cinfo) != DictType): # The 'completed' key is a dictionary of SHA hashes (torrent ids)
                raise ValueError          # ... for keeping track of the total completions per torrent
            for y in cinfo.values():      # ... each torrent has an integer value
                if type(y) not in (IntType,LongType):
                    raise ValueError      # ... for the number of reported completions for that torrent
        elif cname == 'allowed':
            if (type(cinfo) != DictType): # a list of info_hashes and included data
                raise ValueError
            if x.has_key('allowed_dir_files'):
                adlist = [z[1] for z in x['allowed_dir_files'].values()]
                for y in cinfo.keys():        # and each should have a corresponding key here
                    if not y in adlist:
                        raise ValueError
        elif cname == 'allowed_dir_files':
            if (type(cinfo) != DictType): # a list of files, their attributes and info hashes
                raise ValueError
            dirkeys = {}
            for y in cinfo.values():      # each entry should have a corresponding info_hash
                if not y[1]:
                    continue
                if not x['allowed'].has_key(y[1]):
                    raise ValueError
                if dirkeys.has_key(y[1]): # and each should have a unique info_hash
                    raise ValueError
                dirkeys[y[1]] = 1


alas = 'your file may exist elsewhere in the universe\nbut alas, not here\n'

def isotime(secs = None):
    if secs == None:
        secs = bttime()
    return strftime('%Y-%m-%d %H:%M UTC', gmtime(secs))

http_via_filter = re.compile(' for ([0-9.]+)\Z')

def _get_forwarded_ip(headers):
    if headers.has_key('http_x_forwarded_for'):
        header = headers['http_x_forwarded_for']
        try:
            x,y = header.split(',')
        except:
            return header
        if not is_local_ip(x):
            return x
        return y
    if headers.has_key('http_client_ip'):
        return headers['http_client_ip']
    if headers.has_key('http_via'):
        x = http_via_filter.search(headers['http_via'])
        if x.groups > 0:
            return x.group(1)
    if headers.has_key('http_from'):
        return headers['http_from']
    return None

def get_forwarded_ip(headers):
    x = _get_forwarded_ip(headers)
    if x is None or not is_valid_ipv4(x) or is_local_ip(x):
        return None
    return x

def compact_peer_info(ip, port):
    try:
        s = ( ''.join([chr(int(i)) for i in ip.split('.')])
              + chr((port & 0xFF00) >> 8) + chr(port & 0xFF) )
        if len(s) != 6:
            s = ''
    except:
        s = ''  # not a valid IP, must be a domain name
    return s

def is_valid_ipv4(ip):
    a = ip.split('.')
    if len(a) != 4:
        return False
    try:
        for x in a:
            chr(int(x))
        return True
    except:
        return False

def is_local_ip(ip):
    try:
        v = [int(x) for x in ip.split('.')]
        if v[0] == 10 or v[0] == 127 or v[:2] in ([192, 168], [169, 254]):
            return 1
        if v[0] == 172 and v[1] >= 16 and v[1] <= 31:
            return 1
    except ValueError:
        return 0

def params_factory(dictionary, default=None):
    """ 
    Function factory that lets us easily get info from dictionaries of the 
    form { key : [value], ... }
    @param dictionary: the dict to index into
    @param default: the default value to return if key is not found
    @rtype: function
    """
    def params(key, default=default, d=dictionary):
        if d.has_key(key):
            return d[key]
        return default
    return params

class Tracker(object):

    def __init__(self, config, certificate, rawserver):
        self.config = config
        self.response_size = config['response_size']
        self.max_give = config['max_give']
        self.dfile = config['dfile']
        self.natcheck = config['nat_check']
        favicon = config['favicon']
        self.favicon = None
        if favicon:
            try:
                h = open(favicon,'r')
                self.favicon = h.read()
                h.close()
            except:
                print "**warning** specified favicon file -- %s -- does not exist." % favicon
        self.rawserver = rawserver
        self.cached = {}    # format: infohash: [[time1, l1, s1], [time2, l2, s2], [time3, l3, s3]]
        self.cached_t = {}  # format: infohash: [time, cache]
        self.times = {}
        self.state = {}
        self.seedcount = {}
        
        self.certificate = certificate 
        self.networkmodel = NetworkModel()
        
        self.only_local_override_ip = config['only_local_override_ip']
        if self.only_local_override_ip == 2:
            self.only_local_override_ip = not config['nat_check']

        if os.path.exists(self.dfile):
            try:
                h = open(self.dfile, 'rb')
                ds = h.read()
                h.close()
                tempstate = bdecode(ds)
                if not tempstate.has_key('peers'):
                    tempstate = {'peers': tempstate}
                statefiletemplate(tempstate)
                self.state = tempstate
            except:
                print '**warning** statefile '+self.dfile+' corrupt; resetting'
        self.downloads    = self.state.setdefault('peers', {})
        self.completed    = self.state.setdefault('completed', {})

        self.becache = {}   # format: {infohash: [[l1, s1], [l2, s2], [l3, s3]]}
                            # becache[infohash][0]=> Normal => [downloads,seeds]
                            # becache[infohash][1]=> No Peer ID => "" ""
                            # becache[infohash][2]=> Compact => "" ""
        for infohash, ds in self.downloads.items():
            self.seedcount[infohash] = 0
            for x,y in ds.items():
                if not y.get('nat',-1):
                    ip = y.get('given_ip')
                    if not (ip and self.allow_local_override(y['ip'], ip)):
                        ip = y['ip']
                    self.natcheckOK(infohash,x,ip,y['port'],y['left'])
                if not y['left']:
                    self.seedcount[infohash] += 1

        for infohash in self.downloads:
            self.times[infohash] = {}
            for peerid in self.downloads[infohash]:
                self.times[infohash][peerid] = 0

        self.reannounce_interval = config['reannounce_interval']
        self.save_dfile_interval = config['save_dfile_interval']
        self.show_names = config['show_names']
        rawserver.add_task(self.save_dfile, self.save_dfile_interval)
        self.prevtime = bttime()
        self.timeout_downloaders_interval = config['timeout_downloaders_interval']
        rawserver.add_task(self.expire_downloaders, self.timeout_downloaders_interval)
        self.logfile = None
        self.log = None
        if (config['logfile'] != '') and (config['logfile'] != '-'):
            try:
                self.logfile = config['logfile']
                self.log = open(self.logfile,'a')
                sys.stdout = self.log
                print "# Log Started: ", isotime()
            except:
                print "**warning** could not redirect stdout to log file: ", sys.exc_info()[0]

        if config['hupmonitor']:
            def huphandler(signum, frame, self = self):
                try:
                    self.log.close ()
                    self.log = open(self.logfile,'a')
                    sys.stdout = self.log
                    print "# Log reopened: ", isotime()
                except:
                    print "**warning** could not reopen logfile"

            signal.signal(signal.SIGHUP, huphandler)

        self.allow_get = config['allow_get']

        if config['allowed_dir'] != '':
            self.allowed_dir = config['allowed_dir']
            self.parse_dir_interval = config['parse_dir_interval']
            self.allowed = self.state.setdefault('allowed',{})
            self.allowed_dir_files = self.state.setdefault('allowed_dir_files',{})
            self.allowed_dir_blocked = {}
            self.parse_allowed()
        else:
            try:
                del self.state['allowed']
            except:
                pass
            try:
                del self.state['allowed_dir_files']
            except:
                pass
            self.allowed = None

        self.uq_broken = unquote('+') != ' ' # This sucks!
        self.keep_dead = config['keep_dead']

    def allow_local_override(self, ip, given_ip):
        return is_valid_ipv4(given_ip) and (
            not self.only_local_override_ip or is_local_ip(ip) )

    def get_infopage(self):
        try:
            if not self.config['show_infopage']:
                return (404, 'Not Found', {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, alas)
            red = self.config['infopage_redirect']
            if red != '':
                return (302, 'Found', {'Content-Type': 'text/html', 'Location': red},
                        '<A HREF="'+red+'">Click Here</A>')

            s = StringIO()
            s.write('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n' \
                '<html><head><title>Anomos download info</title>\n')
            if self.favicon is not None:
                s.write('<link rel="shortcut icon" href="/favicon.ico">\n')
            s.write('</head>\n<body>\n' \
                '<h3>Anomos download info</h3>\n'\
                '<ul>\n'
                '<li><strong>tracker version:</strong> %s</li>\n' \
                '<li><strong>server time:</strong> %s</li>\n' \
                '</ul>\n' % (version, isotime()))
            if self.allowed is not None:
                if self.show_names:
                    names = [ (value['name'], infohash)
                              for infohash, value in self.allowed.iteritems()]
                else:
                    names = [(None, infohash) for infohash in self.allowed]
            else:
                names = [ (None, infohash) for infohash in self.downloads]
            if not names:
                s.write(str(self.allowed))
                s.write(str(self.downloads))
                s.write('<p>not tracking any files yet...</p>\n')
            else:
                names.sort()
                tn = 0
                tc = 0
                td = 0
                tt = 0  # Total transferred
                ts = 0  # Total size
                nf = 0  # Number of files displayed
                if self.allowed is not None and self.show_names:
                    s.write('<table summary="files" border="1">\n' \
                        '<tr><th>info hash</th><th>torrent name</th><th align="right">size</th><th align="right">complete</th><th align="right">downloading</th><th align="right">downloaded</th><th align="right">transferred</th></tr>\n')
                else:
                    s.write('<table summary="files">\n' \
                        '<tr><th>info hash</th><th align="right">complete</th><th align="right">downloading</th><th align="right">downloaded</th></tr>\n')
                for name, infohash in names:
                    l = self.downloads[infohash]
                    n = self.completed.get(infohash, 0)
                    tn += n
                    c = self.seedcount[infohash]
                    tc += c
                    d = len(l) - c
                    td += d
                    if self.allowed is not None and self.show_names:
                        if self.allowed.has_key(infohash):
                            nf += 1
                            sz = self.allowed[infohash]['length']  # size
                            ts += sz
                            szt = sz * n   # Transferred for this torrent
                            tt += szt
                            if self.allow_get == 1:
                                linkname = '<a href="/file?info_hash=' + quote(infohash) + '">' + name + '</a>'
                            else:
                                linkname = name
                            s.write('<tr><td><code>%s</code></td><td>%s</td><td align="right">%s</td><td align="right">%i</td><td align="right">%i</td><td align="right">%i</td><td align="right">%s</td></tr>\n' \
                                % (b2a_hex(infohash), linkname, size_format(sz), c, d, n, size_format(szt)))
                    else:
                        s.write('<tr><td><code>%s</code></td><td align="right"><code>%i</code></td><td align="right"><code>%i</code></td><td align="right"><code>%i</code></td></tr>\n' \
                            % (b2a_hex(infohash), c, d, n))
                ttn = 0
                for i in self.completed.values():
                    ttn += i
                if self.allowed is not None and self.show_names:
                    s.write('<tr><td align="right" colspan="2">%i files</td><td align="right">%s</td><td align="right">%i</td><td align="right">%i</td><td align="right">%i/%i</td><td align="right">%s</td></tr>\n'
                            % (nf, size_format(ts), tc, td, tn, ttn, size_format(tt)))
                else:
                    s.write('<tr><td align="right">%i files</td><td align="right">%i</td><td align="right">%i</td><td align="right">%i/%i</td></tr>\n'
                            % (nf, tc, td, tn, ttn))
                s.write('</table>\n' \
                    '<ul>\n' \
                    '<li><em>info hash:</em> SHA1 hash of the "info" section of the metainfo (*.torrent)</li>\n' \
                    '<li><em>complete:</em> number of connected clients with the complete file</li>\n' \
                    '<li><em>downloading:</em> number of connected clients still downloading</li>\n' \
                    '<li><em>downloaded:</em> reported complete downloads (total: current/all)</li>\n' \
                    '<li><em>transferred:</em> torrent size * total downloaded (does not include partial transfers)</li>\n' \
                    '</ul>\n')

            s.write('</body>\n' \
                '</html>\n')
            return (200, 'OK', {'Content-Type': 'text/html; charset=iso-8859-1'}, s.getvalue())
        except:
            print_exc()
            return (500, 'Internal Server Error', {'Content-Type': 'text/html; charset=iso-8859-1'}, 'Server Error')

    def scrapedata(self, infohash, return_name = True):
        l = self.downloads[infohash]
        n = self.completed.get(infohash, 0)
        c = self.seedcount[infohash]
        d = len(l) - c
        f = {'complete': c, 'incomplete': d, 'downloaded': n}
        if return_name and self.show_names and self.allowed is not None:
            f['name'] = self.allowed[infohash]['name']
        return f

    def get_scrape(self, paramslist):
        params = params_factory(paramslist)
        fs = {}
        if params('info_hash'):
            if self.config['scrape_allowed'] not in ['specific', 'full']:
                return self.reply(400, 'Not Authorized', \
                    {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, \
                    bencode({'failure reason': 'specific scrape function is not available with this tracker.'}), \
                    params('peer_id'))
            for infohash in params('info_hash'):
                if self.allowed is not None and infohash not in self.allowed:
                    continue
                if infohash in self.downloads:
                    fs[infohash] = self.scrapedata(infohash)
        else:
            if self.config['scrape_allowed'] != 'full':
                return self.reply(400, 'Not Authorized', \
                    {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, \
                    bencode({'failure reason': 'full scrape function is not available with this tracker.'}), \
                    params('peer_id'))
            if self.allowed is not None:
                hashes = self.allowed
            else:
                hashes = self.downloads
            for infohash in hashes:
                fs[infohash] = self.scrapedata(infohash)
        return self.reply(200, 'OK', {'Content-Type': 'text/plain'}, bencode({'files': fs}), params('peer_id'))

    def get_file(self, infohash):
         if not self.allow_get:
             return (400, 'Not Authorized', {'Content-Type': 'text/plain', 'Pragma': 'no-cache'},
                 'get function is not available with this tracker.')
         if not self.allowed.has_key(infohash):
             return (404, 'Not Found', {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, alas)
         fname = self.allowed[infohash]['file']
         fpath = self.allowed[infohash]['path']
         return (200, 'OK', {'Content-Type': 'application/x-bittorrent',
             'Content-Disposition': 'attachment; filename=' + fname},
             open(fpath, 'rb').read())

    def check_allowed(self, infohash, paramslist):
        params = params_factory(paramslist)
        if self.allowed is not None:
            if not self.allowed.has_key(infohash):
                return self.reply(200, 'Not Authorized', \
                    {'Content-Type': 'text/plain', 'Pragma': 'no-cache'},\
                    bencode({'failure reason': 'Requested download is not authorized for use with this tracker.'}), \
                    params('peer_id'))
            if self.config['allowed_controls']:
                if self.allowed[infohash].has_key('failure reason'):
                    return self.reply(200, 'Not Authorized', \
                        {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, \
                        bencode({'failure reason': self.allowed[infohash]['failure reason']}), \
                        params('peer_id'))
        return None
    
    def update_simpeer(self, paramslist, ip, peercert):
        params = params_factory(paramslist)
        peerid = params('peer_id')
        simpeer = self.networkmodel.get(peerid)
        if not simpeer:
            #if params('pubkey'): # New peer
            loc = (ip, int(params('port')))
            simpeer = self.networkmodel.addPeer(params('peer_id'), 
                                                    peercert, loc)
        elif params('event') == 'stopped':
            self.networkmodel.disconnect(peerid)
        # TODO: What if they don't give a pubkey
        #Verify the connecting peer is who they say they are.
        #Update any changed information
    
    def add_data(self, infohash, event, ip, paramslist):
        peers = self.downloads.setdefault(infohash, {})
        ts = self.times.setdefault(infohash, {})
        self.completed.setdefault(infohash, 0)
        self.seedcount.setdefault(infohash, 0)

        params = params_factory(paramslist)

        myid = params('peer_id','')
        if len(myid) != 20:
            raise ValueError('id not of length 20')
        if event not in ['started', 'completed', 'stopped', None]:
            raise ValueError('invalid event')
        port = int(params('port',''))
        if not (0 < port < 65535):
            raise ValueError('invalid port')
        left = int(params('left',''))
        if left < 0:
            raise ValueError('invalid amount left')

        peer = peers.get(myid)
        #I'm getting rid of the old key field, it was used as a quasi identity
        #for each peer but it's obsoleted by signing and encryption.
        #mykey = params('key')
        #auth = not peer or peer.get('key', -1) == mykey or peer.get('ip') == ip
        auth = not peer or peer.get('ip') == ip
        
        gip = params('ip')
        local_override = gip and self.allow_local_override(ip, gip)
        if local_override:
            ip1 = gip
        else:
            ip1 = ip
        if not auth and local_override and self.only_local_override_ip:
            auth = True

        if params('numwant') is not None:
            rsize = min(int(params('numwant')), self.max_give)
        else:
            rsize = self.response_size

        if event == 'stopped':
            if peer and auth:
                self.delete_peer(infohash,myid)

        elif not peer:
            ts[myid] = bttime()
            peer = {'ip': ip, 'port': port, 'left': left}
            if gip:
                peer['given ip'] = gip
            if port:
                if not self.natcheck or (local_override and self.only_local_override_ip):
                    peer['nat'] = 0
                    self.natcheckOK(infohash,myid,ip1,port,left)
                else:
                    NatCheck(self.connectback_result,infohash,myid,ip1,port,self.rawserver)
            else:
                peer['nat'] = 2**30
            if event == 'completed':
                self.completed[infohash] += 1
            if not left:
                self.seedcount[infohash] += 1

            peers[myid] = peer

        else:
            if not auth:
                return rsize    # return w/o changing stats

            ts[myid] = bttime()
            if not left and peer['left']:
                # Peer has a complete file, count them as a seeder.
                self.completed[infohash] += 1
                self.seedcount[infohash] += 1
                if not peer.get('nat', -1):
                    # Move their becache data from downloader to seeder array.
                    for bc in self.becache[infohash]:
                        bc[1][myid] = bc[0][myid]
                        del bc[0][myid]
            if peer['left']:
                peer['left'] = left

            recheck = False
            if ip != peer['ip']:
                peer['ip'] = ip
                recheck = True
            if gip != peer.get('given ip'):
                if gip:
                    peer['given ip'] = gip
                elif peer.has_key('given ip'):
                    del peer['given ip']
                if local_override:
                    if self.only_local_override_ip:
                        self.natcheckOK(infohash,myid,ip1,port,left)
                    else:
                        recheck = True

            if port and self.natcheck:
                if recheck:
                    if peer.has_key('nat'):
                        if not peer['nat']:
                            l = self.becache[infohash]
                            y = not peer['left']
                            for x in l:
                                del x[y][myid]
                        del peer['nat'] # restart NAT testing
                else:
                    natted = peer.get('nat', -1)
                    if natted and natted < self.natcheck:
                        recheck = True

                if recheck:
                    NatCheck(self.connectback_result,infohash,myid,ip1,port,self.rawserver)

        return rsize
    
    def neighborlist(self, peerid):
        sim = self.networkmodel.get(peerid)
        if sim and not sim.id_map:
            return []
        neighbors = []
        for p in sim.id_map.values():
            loc = sim.neighbors[p]['loc']
            nid = sim.neighbors[p]['nid']
            neighbors.append({'ip':loc[0], 'port':loc[1], 'peer id':nid})
        return neighbors
    
    def getTCs(self, peerid, infohash, return_type, count):
        """
        Gets a set of tracking codes from the specified peer to 'count' random
        peers with 'infohash'.
        @param peerid: PeerID of requesting peer
        @param infohash: Infohash of requested file
        @param count: Number of peers requested
        @param return_type: Temporary arg for BT compatibility, will be removed.
        @type peerid: str
        @type infohash: str
        @type count: int
        """
        #TODO: Update the cache system, and only get peers in infohash's swarm
        #cache = self.cached.setdefault(infohash,[None,None,None])[return_type]
        allpeers = self.networkmodel.getNames()
        tcs = []
        for id in sample(allpeers, min(len(allpeers), count)):
            if id == peerid:
                continue
            print id, "To", peerid
            aes = AESKey()
            #TODO: design a good format for the request
            t = self.networkmodel.getTrackingCode(peerid, id, infohash + aes.key + aes.iv)
            if t:
                tcs.append([aes.key + aes.iv, t])
        return tcs
        
        
#    def peerlist(self, peerid, infohash, stopped, is_seed, return_type, rsize):
#        """ Return a set of Tracking Codes 
#        @param peerid: PeerID of source peer
#        @param infohash: File requested
#        @param stopped: 
#        """
#        data = {}    # data to be returned
#        seeds = self.seedcount[infohash]
#        data['complete'] = seeds
#        data['incomplete'] = len(self.downloads[infohash]) - seeds

#        if ( self.allowed is not None and self.config['allowed_controls'] and
#                                self.allowed[infohash].has_key('warning message') ):
#            data['warning message'] = self.allowed[infohash]['warning message']

#        data['interval'] = self.reannounce_interval
#        if stopped or not rsize:     # save some bandwidth
#            data['peers'] = []
#            return data
#        
#        bc = self.becache.setdefault(infohash,[[{}, {}], [{}, {}], [{}, {}]])
#        len_l = len(bc[0][0]) # Number of downloaders
#        len_s = len(bc[0][1]) # Number of seeders
#        if not (len_l+len_s):   # caches are empty!
#            data['peers'] = []
#            return data
#        l_get_size = int(float(rsize)*(len_l)/(len_l+len_s))
#        cache = self.cached.setdefault(infohash,[None,None,None])[return_type]
#        if cache:
#            if cache[0] + self.config['min_time_between_cache_refreshes'] < bttime():
#                cache = None
#            else:
#                if ( (is_seed and len(cache[1]) < rsize)
#                     or len(cache[1]) < l_get_size or not cache[1] ):
#                        cache = None
#        if not cache:
#            vv = [[],[],[]]
#            cache = [ bttime(),
#                      bc[return_type][0].values()+vv[return_type],
#                      bc[return_type][1].values() ]
#            shuffle(cache[1])
#            shuffle(cache[2])
#            self.cached[infohash][return_type] = cache
#            for rr in xrange(len(self.cached[infohash])):
#                if rr != return_type:
#                    try:
#                        self.cached[infohash][rr][1].extend(vv[rr])
#                    except:
#                        pass
#        if len(cache[1]) < l_get_size:
#            peerdata = cache[1]
#            if not is_seed:
#                peerdata.extend(cache[2])
#            cache[1] = []
#            cache[2] = []
#        else:
#            if not is_seed:
#                peerdata = cache[2][l_get_size-rsize:]
#                del cache[2][l_get_size-rsize:]
#                rsize -= len(peerdata)
#            else:
#                peerdata = []
#            if rsize:
#                peerdata.extend(cache[1][-rsize:])
#                del cache[1][-rsize:]
#        if return_type == 2:
#            peerdata = ''.join(peerdata)
#        data['peers'] = peerdata
#        return data

    def validate_request(self, paramslist):
        params = params_factory(paramslist)
        infohash = params('info_hash')
        if infohash and len(infohash) != 20:
            raise ValueError('infohash not of length 20')
        if len(params('peer_id', '')) != 20:
            raise ValueError('id not of length 20')
        if params('event') not in ['started', 'completed', 'stopped', None]:
            raise ValueError('invalid event')
        port = int(params('port',-1))
        if not (0 < port < 65535):
            raise ValueError('invalid or unspecified port')
        left = params('left')
        if left and int(left) < 0:
            raise ValueError('invalid amount left')

    def get(self, handler, path, headers):
        ip = handler.get_ip()

        nip = get_forwarded_ip(headers)
        if nip and not self.only_local_override_ip:
            ip = nip
        
        paramslist = {}
        params = params_factory(paramslist)
        
        try: 
            (scheme, netloc, path, pars, query, fragment) = urlparse(path)
            if self.uq_broken == 1:
                path = path.replace('+',' ')
                # query = query.replace('+',' ')
            path = unquote(path)[1:]
            paramslist.update(self.parseQuery(query))
#            if params('pke'):
#                # Decrypt the query
#                binpke = urlsafe_b64decode(params('pke'))
#                try:
#                    decquery = self.rsa.decrypt(binpke, returnpad=False)
#                except CryptoError, e:
#                    raise ValueError(e)
#                # Update with the new params
#                paramslist.update(self.parseQuery(decquery))
#                del paramslist['pke']
#            
            if path == '' or path == 'index.html':
                return self.get_infopage()
            if path == 'scrape':
                return self.get_scrape(paramslist)
            #if (path == 'key'):
            #    pubic = self.rsa.pub_bin()
            #    return (200, 'OK', {'Content-Type' : 'text/plain'}, pubic)
            if (path == 'file'):
                return self.get_file(params('info_hash'))
            if path == 'favicon.ico' and self.favicon is not None:
                return (200, 'OK', {'Content-Type' : 'image/x-icon'}, self.favicon)
            if path != 'announce':
                return (404, 'Not Found', {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, alas)

            context = handler.connection.socket.get_context()
            self.update_simpeer(paramslist, ip, handler.connection.peer_cert)
            self.validate_request(paramslist)
            # main tracker function
            infohash = params('info_hash')
            #Quite alright not to send an infohash now.
            #if not infohash:
            #    raise ValueError('no info hash')

            notallowed = self.check_allowed(infohash, paramslist)
            if notallowed:
                return notallowed
            event = params('event')

            #rsize = self.add_data(infohash, event, ip, paramslist)

        except ValueError, e:
            return self.reply(400, 'Bad Request', {'Content-Type': 'text/plain'},
                'you sent me garbage - ' + str(e), params('peer_id'))

        if params('compact'):
            return_type = 2
        elif params('no_peer_id'):
            return_type = 1
        else:
            return_type = 0
        
        stopped = event == 'stopped'
        data = {}
        if not stopped:
            data['peers'] = self.neighborlist(params('peer_id'))
            #TODO: Replace "3" with actual number of TCs to get
            if params('left') and int(params('left')):
                data['tracking codes'] = self.getTCs(params('peer_id'), infohash, return_type, 3)
            else:
                data['tracking codes'] = []
        #self.peerlist(infohash, event=='stopped',  not params('left'), return_type, rsize)

        if paramslist.has_key('scrape'):
            data['scrape'] = self.scrapedata(infohash, False)
        return self.reply(200, 'OK', {'Content-Type': 'text/plain', 'Pragma': 'no-cache'}, bencode(data), params('peer_id'))

    def parseQuery(self, query):
        if len(query) and query[0] == '?':
            query = query[1:]
        params = {}
        for s in query.split('&'):
            if s != '':
                key,val = s.split('=', 1) #Only split at the first "=" character
                kw = unquote(key)
                kw = kw.replace('+',' ') # TODO: find out if this is absolutely necessary
                params[kw] = unquote(val)
        return params

    def natcheckOK(self, infohash, peerid, ip, port, not_seed):
        bc = self.becache.setdefault(infohash,[[{}, {}], [{}, {}], [{}, {}]])
        bc[0][not not_seed][peerid] = Bencached(bencode({'ip': ip, 'port': port,
                                              'peer id': peerid}))
        bc[1][not not_seed][peerid] = Bencached(bencode({'ip': ip, 'port': port}))
        bc[2][not not_seed][peerid] = compact_peer_info(ip, port)

    def natchecklog(self, peerid, ip, port, result):
        
        print '%s - %s [%02d/%3s/%04d:%02d:%02d:%02d] "!natcheck-%s:%i" %i 0 - -' % (
            ip, quote(peerid), strftime("[%d/%b/%Y:%H:%M:%S]"), ip, port, result)

    def connectback_result(self, result, downloadid, peerid, ip, port):
        record = self.downloads.get(downloadid, {}).get(peerid)
        if ( record is None
                 or (record['ip'] != ip and record.get('given ip') != ip)
                 or record['port'] != port ):
            if self.config['log_nat_checks']:
                self.natchecklog(peerid, ip, port, 404)
            return
        if self.config['log_nat_checks']:
            if result:
                x = 200
            else:
                x = 503
            self.natchecklog(peerid, ip, port, x)
        if not record.has_key('nat'):
            record['nat'] = int(not result)
            if result:
                self.natcheckOK(downloadid,peerid,ip,port,record['left'])
        elif result and record['nat']:
            record['nat'] = 0
            self.natcheckOK(downloadid,peerid,ip,port,record['left'])
        elif not result:
            record['nat'] += 1

    def save_dfile(self):
        self.rawserver.add_task(self.save_dfile, self.save_dfile_interval)
        h = open(self.dfile, 'wb')
        h.write(bencode(self.state))
        h.close()

    def parse_allowed(self):
        self.rawserver.add_task(self.parse_allowed, self.parse_dir_interval)

        # logging broken .torrent files would be useful but could confuse
        # programs parsing log files, so errors are just ignored for now
        def ignore(message):
            pass
        r = parsedir(self.allowed_dir, self.allowed, self.allowed_dir_files,
                     self.allowed_dir_blocked, ignore,include_metainfo = False)
        ( self.allowed, self.allowed_dir_files, self.allowed_dir_blocked,
          added, garbage2 ) = r

        for infohash in added:
            self.downloads.setdefault(infohash, {})
            self.completed.setdefault(infohash, 0)
            self.seedcount.setdefault(infohash, 0)

        self.state['allowed'] = self.allowed
        self.state['allowed_dir_files'] = self.allowed_dir_files

    def delete_peer(self, infohash, peerid):
        dls = self.downloads[infohash]
        peer = dls[peerid]
        if not peer['left']:
            self.seedcount[infohash] -= 1
        if not peer.get('nat',-1):
            l = self.becache[infohash]
            y = not peer['left']
            for x in l:
                del x[y][peerid]
        del self.times[infohash][peerid]
        del dls[peerid]

    def expire_downloaders(self):
        for infohash, peertimes in self.times.items():
            for myid, t in peertimes.items():
                if t < self.prevtime:
                    self.delete_peer(infohash, myid)
        self.prevtime = bttime()
        if (self.keep_dead != 1):
            for key, peers in self.downloads.items():
                if len(peers) == 0 and (self.allowed is None or
                                        key not in self.allowed):
                    del self.times[key]
                    del self.downloads[key]
                    del self.seedcount[key]
        self.rawserver.add_task(self.expire_downloaders, self.timeout_downloaders_interval)
    
    def reply(self, code, message, headers, data, peer_id=None):
        """ 
        Craft an HTTP response.
        If peer_id is present this function will attempt to encrypt the data.
        @return: HTTP response, eg. (200, OK, {'Content-Type': 'text/plain'}, data)
        @rtype: tuple
        """
#        if peer_id: # Try to encrypt the response
#            simpeer = self.networkmodel.get(peer_id)
#            if simpeer and simpeer.pubkey:
#                if not isinstance(data, basestring):
#                    data = bencode(data)
#                data = bencode({'pke': simpeer.pubkey.encrypt(data)})
        return (code, message, headers, data)

def track(args):
    if len(args) == 0:
        print formatDefinitions(defaults, 80)
        return
    try:
        config, files = parseargs(args, defaults, 0, 0)
    except ValueError, e:
        print 'error: ' + str(e)
        print 'run with no arguments for parameter explanations'
        return

    initCrypto(config['data_dir'])
    servercert = Certificate("server") #XXX: make the name user defined
    r = RawServer(Event(), config, servercert, bindaddr = config['bind'])
    t = Tracker(config, servercert, r)
    s = r.create_ssl_serversocket(config['port'], config['bind'], True)
    r.start_listening(s, HTTPHandler(t.get, config['min_time_between_log_flushes']))
    r.listen_forever()
    t.save_dfile()
    print '# Shutting down: ' + isotime()

def size_format(s):
    if (s < 1024):
        r = "%d B" % int(s)
    elif (s < 1048576):
        r = "%.2f KiB" % (s/1024.0)
    elif (s < 1073741824):
        r = "%.2f MiB" % (s/1048576.0)
    elif (s < 1099511627776):
        r = "%.2f GiB" % (s/1073741824.0)
    else:
        r = "%.2f TiB" % (s/1099511627776.0)
    return r

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

# Written by Bram Cohen and Uoti Urpala, modified by Anomos Liberty
# Enhancements

import os
import sys
import threading
import gc
from cStringIO import StringIO
from traceback import print_exc
from math import sqrt

from Anomos.Choker import Choker
from Anomos.ConvertedMetainfo import set_filesystem_encoding
from Anomos.Measure import Measure
from Anomos.Downloader import Downloader
from Anomos.DownloaderFeedback import DownloaderFeedback
from Anomos.EndPoint import EndPoint
from Anomos.NeighborManager import NeighborManager
from Anomos.PiecePicker import PiecePicker
from Anomos.RateLimiter import RateLimiter
from Anomos.RateMeasure import RateMeasure
from Anomos.Rerequester import Rerequester
from Anomos.SingleportListener import SingleportListener
from Anomos.Storage import Storage, FilePool
from Anomos.StorageWrapper import StorageWrapper
from Anomos.Torrent import Torrent
from Anomos.Uploader import Upload
from Anomos import bttime, version, LOG as log
from Anomos import BTFailure, BTShutdown

import Anomos.Crypto

from Anomos.EventHandler import EventHandler

from M2Crypto.SSL import SSLError

class Feedback(object):

    def finished(self, torrent):
        pass

    def failed(self, torrent, is_external):
        pass

    def error(self, torrent, level, text):
        pass

    def exception(self, torrent, text):
        self.error(torrent, CRITICAL, text)

    def started(self, torrent):
        pass


class Multitorrent(object):

    def __init__(self, config, doneflag, listen_fail_ok=False):
        self.config = dict(config)
        Anomos.Crypto.init(self.config['data_dir'])
        self.cert_flag = threading.Event()
        self.event_handler = EventHandler(doneflag)
        self.schedule = self.event_handler.schedule
        self.filepool = FilePool(config['max_files_open'])
        self.ratelimiter = RateLimiter(self.schedule)
        self.ratelimiter.set_parameters(config['max_upload_rate'],
                                        config['upload_unit_size'])
        self.nbr_mngrs = {}
        self.torrents = {}
        set_filesystem_encoding(config['filesystem_encoding'])
        self.listen_fail_ok = listen_fail_ok

        # If the user supplies an identify from the configuration, use this
        # for all connections. If not, use only ephemeral certificates,
        # generated in their own thread.
        # TODO: Allow users who supply an identity to provide different
        # identities to different trackers.
        if self.config['identity'] not in ['', None]:
            self.certificate = Anomos.Crypto.Certificate(loc=self.config['identity'], \
                                                          ephemeral=False)
            self.post_certificate_load()
        else:
            self.certificate = None
            self.certificates = {}

        # This dictionary contains everything necessary to maintain connections
        # to numerous trackers and their surrounding networks.
        # {announce_url: [NeighborManager, Certificate, SessionID,
        # SSL Context, SingleportListener]}
        self.trackers = {}

    def close_listening_socket(self):
        for aurl, info in self.trackers.items():
            info[4].close_sockets()

    def start_torrent(self, metainfo, config, feedback, filename,callback=None):

        if not self.cert_flag.isSet(): 
            if hasattr(metainfo, "announce_list"):
                t = threading.Thread(target=self.gen_keep_certs,
                args=(metainfo.announce_list,))
                t.start()
                self.schedule(1,
                        lambda:
                            self.try_start_torrent(metainfo, config, feedback, filename, callback),
                        context=None)
            else:
                threading.Thread(target=self.gen_keep_certs,
                        args=[[[metainfo.announce]]]).start()
                self.schedule(1,
                        lambda:
                            self.try_start_torrent(metainfo, config, feedback, filename, callback),
                        context=None)
        else:
            if self.certificate is not None:
                aurl = metainfo.announce
                if not self.trackers.has_key(aurl):
                    self.trackers[aurl] = [None, None, None, None, None]
                if self.trackers[aurl][1] is None:
                    log.info("Generating a new certificate")
                    self.trackers[aurl][1]= self.certificate
                    self.trackers[aurl][2]= self.sessionid
                    self.trackers[aurl][3]= self.ssl_ctx
                    self.trackers[aurl][4]= self.singleport_listener
                    self.trackers[aurl][4].find_port(self.listen_fail_ok)
                    nbr = NeighborManager(self.config,
                            self.trackers[aurl][1], \
                            self.trackers[aurl][3], self.trackers[aurl][2], \
                            self.schedule, self.ratelimiter)
                    self.nbr_mngrs[aurl] = nbr
                    self.trackers[aurl][0] = nbr
            self.try_start_torrent(metainfo, config, feedback, filename, callback)

    def try_start_torrent(self, metainfo, config, feedback, filename,callback=None):

        if not self.cert_flag.isSet():
                self.schedule(1,
                        lambda:
                            self.try_start_torrent(metainfo, config, feedback, filename, callback),
                        context=None)
                return

        torrent = _SingleTorrent(self.event_handler, \
                                 self.trackers,\
                                 self.ratelimiter, self.filepool, config,\
                                 )
        self.event_handler.add_context(torrent)
        self.torrents[metainfo.infohash] = torrent
        def start():
            torrent.start_download(metainfo, feedback, filename)
        self.schedule(0, start, context=torrent)
        self.cert_flag = threading.Event()
        if callback is not None:
            callback(torrent)
        else:
            return torrent

    def gen_cert(self):
        self.certificate = Anomos.Crypto.Certificate(ephemeral=True)
        self.post_certificate_load()

    def post_certificate_load(self):
        self.sessionid = Anomos.Crypto.get_rand(8)
        self.ssl_ctx = self.certificate.get_ctx(allow_unknown_ca=True)
        self.singleport_listener = SingleportListener(self.config, self.ssl_ctx)
        self.singleport_listener.find_port(self.listen_fail_ok)
        self.cert_flag.set()

    def gen_keep_certs(self, announce_list):
        # TODO: Make this compatible with BEP12, allowing tracker load
        # balancing
        for aurl_list in announce_list:
                for aurl in aurl_list:
                    if not self.trackers.has_key(aurl):
                        self.trackers[aurl] = [None, None, None, None, None]
                    if self.trackers[aurl][1] is None:
                        log.info("Generating a new certificate")
                        self.trackers[aurl][1]= Anomos.Crypto.Certificate(ephemeral=True)
                        self.trackers[aurl][2]= Anomos.Crypto.get_rand(8)
                        self.trackers[aurl][3]= self.trackers[aurl][1].get_ctx(allow_unknown_ca=True)
                        self.trackers[aurl][4]= SingleportListener(self.config,
                                self.trackers[aurl][3])
                        self.trackers[aurl][4].find_port(self.listen_fail_ok)
                        nbr = NeighborManager(self.config,
                                self.trackers[aurl][1], \
                                self.trackers[aurl][3], self.trackers[aurl][2], \
                                self.schedule, self.ratelimiter)
                        self.nbr_mngrs[aurl] = nbr
                        self.trackers[aurl][0] = nbr
        self.cert_flag.set()

    def set_option(self, option, value):
        if option not in self.config or self.config[option] == value:
            return
        if option not in 'max_upload_rate upload_unit_size '\
               'max_files_open minport maxport'.split():
            return
        self.config[option] = value
        if option == 'max_files_open':
            self.filepool.set_max_files_open(value)
        elif option == 'max_upload_rate':
            self.ratelimiter.set_parameters(value,
                                            self.config['upload_unit_size'])
        elif option == 'upload_unit_size':
            self.ratelimiter.set_parameters(self.config['max_upload_rate'],
                                            value)
        elif option == 'maxport':
            for aurl, info in self.trackers.items():
                port = info[4].port
                if not self.config['minport'] <= port <= \
                       self.config['maxport']:
                    info[4].find_port()

    def get_completion(self, config, metainfo, save_path, filelist=False):
        if not config['data_dir']:
            return None
        infohash = metainfo.infohash
        if metainfo.is_batch:
            myfiles = [os.path.join(save_path, f) for f in metainfo.files_fs]
        else:
            myfiles = [save_path]

        if metainfo.file_size == 0:
            if filelist:
                return None
            return 1
        try:
            s = Storage(None, None, zip(myfiles, metainfo.sizes),
                        check_only=True)
        except:
            return None
        filename = os.path.join(config['data_dir'], 'resume',
                                infohash.encode('hex'))
        try:
            f = file(filename, 'rb')
        except:
            f = None
        try:
            r = s.check_fastresume(f, filelist, metainfo.piece_length,
                                   len(metainfo.hashes), myfiles)
        except:
            r = None
        if f is not None:
            f.close()
        if r is None:
            return None
        if filelist:
            return r[0] / metainfo.file_size, r[1], r[2]
        return r / metainfo.file_size

class _SingleTorrent(object):

    def __init__(self, event_handler, trackers, ratelimiter, filepool,
                 config):
        self.event_handler = event_handler
        self._ratelimiter = ratelimiter
        self._filepool = filepool
        self.config = dict(config)
        self._storage = None
        self._storagewrapper = None
        self._ratemeasure = None
        self._upmeasure = None
        self._downmeasure = None
        self._torrent = None
        self._statuscollecter = None
        self._announced = False
        self._listening = False
        self.reserved_ports = []
        self.reported_ports = []
        self._myfiles = None
        self.started = False
        self.is_seed = False
        self.closed = False
        self.infohash = None
        self.file_size = None
        self._doneflag = threading.Event()
        self.finflag = threading.Event()
        self._hashcheck_thread = None
        self._contfunc = None
        self._activity = ('Initial startup', 0)
        self.feedback = None
        self.messages = []
        self.rerequesters = []
        self.trackers = trackers

    def schedule(self, delay, func):
        self.event_handler.schedule(delay, func, context=self)

    def start_download(self, *args, **kwargs):
        it = self._start_download(*args, **kwargs)
        def cont():
            try:
                it.next()
            except StopIteration:
                self._contfunc = None
        def contfunc():
            self.schedule(0, cont)
        self._contfunc = contfunc
        contfunc()

    def _start_download(self, metainfo, feedback, save_path):
        # GTK Crash Hack
        import time
        time.sleep(.2)

        self.feedback = feedback
        self._set_auto_uploads()
        self.metainfo = metainfo

        self.infohash = metainfo.infohash
        self.file_size = metainfo.file_size
        if not metainfo.reported_errors:
            metainfo.show_encoding_errors(log.error)

        if metainfo.is_batch:
            myfiles = [os.path.join(save_path, f) for f in metainfo.files_fs]
        else:
            myfiles = [save_path]
        self._filepool.add_files(myfiles, self)
        self._myfiles = myfiles
        self._storage = Storage(self.config, self._filepool, zip(myfiles,
                                                            metainfo.sizes))
        resumefile = None
        if self.config['data_dir']:
            filename = os.path.join(self.config['data_dir'], 'resume',
                                    self.infohash.encode('hex'))
            if os.path.exists(filename):
                try:
                    resumefile = file(filename, 'rb')
                    if self._storage.check_fastresume(resumefile) == 0:
                        resumefile.close()
                        resumefile = None
                except Exception, e:
                    log.info("Could not load fastresume data: "+
                                str(e) + ". Will perform full hash check.")
                    if resumefile is not None:
                        resumefile.close()
                    resumefile = None
        def data_flunked(amount, index):
            self._ratemeasure.data_rejected(amount)
            log.info('piece %d failed hash check, '
                        're-downloading it' % index)
        backthread_exception = None
        def hashcheck():
            def statusfunc(activity = None, fractionDone = 0):
                if activity is None:
                    activity = self._activity[0]
                self._activity = (activity, fractionDone)
            try:
                self._storagewrapper = StorageWrapper(self._storage,
                     self.config, metainfo.hashes, metainfo.piece_length,
                     self._finished, statusfunc, self._doneflag, data_flunked,
                     self.infohash, resumefile)
            except:
                backthread_exception = sys.exc_info()
            self._contfunc()
        thread = threading.Thread(target = hashcheck)
        thread.setDaemon(False)
        self._hashcheck_thread = thread
        thread.start()
        yield None
        self._hashcheck_thread = None
        if resumefile is not None:
            resumefile.close()
        if backthread_exception:
            a, b, c = backthread_exception
            raise a, b, c

        if self._storagewrapper.amount_left == 0:
            self._finished()
        choker = Choker(self.config, self.schedule, self.finflag.isSet)
        upmeasure = Measure(self.config['max_rate_period'])
        downmeasure = Measure(self.config['max_rate_period'])
        self._upmeasure = upmeasure
        self._downmeasure = downmeasure
        self._ratemeasure = RateMeasure(self._storagewrapper.amount_left_with_partials)
        picker = PiecePicker(len(metainfo.hashes), self.config)
        for i in xrange(len(metainfo.hashes)):
            if self._storagewrapper.do_I_have(i):
                picker.complete(i)
        for i in self._storagewrapper.stat_dirty:
            picker.requested(i)
        def kickpeer(connection):
            def kick():
                connection.close()
            self.schedule(0, kick)
        downloader = Downloader(self.config, self._storagewrapper, picker,
                                len(metainfo.hashes), downmeasure,
                                self._ratemeasure.data_came_in, kickpeer)
        def make_upload(connection):
            return Upload(connection, self._ratelimiter, upmeasure, choker,
                    self._storagewrapper, self.config['max_slice_length'],
                    self.config['max_rate_period'])
        self._torrent = Torrent(self.infohash, make_upload,
                                downloader, len(metainfo.hashes), self)
        self.reported_port = self.config['forwarded_port'] # This is unlikely.
        if not self.reported_port:
            for aurl, info in self.trackers.items():
                self.reported_port = info[4].get_port(info[0])
                self.reserved_ports.append(self.reported_port)
        else:
            self.reported_ports.append(self.reported_port)
        for aurl, info in self.trackers.items():
            info[0].add_torrent(self.infohash, self._torrent)
        self._listening = True
        if hasattr(metainfo, "announce_list"):
            for aurl_list in metainfo.announce_list:
                for aurl in aurl_list:
                    self.rerequesters.append(Rerequester(aurl, self.config,
                    self.schedule, self.trackers[aurl][0], self._storagewrapper.get_amount_left,
                    upmeasure.get_total, downmeasure.get_total, info[4].get_port(info[0]),
                    self.infohash, self.finflag, self.internal_shutdown,
                    self._announce_done, self.trackers[aurl][1],
                    self.trackers[aurl][2]))
        else:
            aurl = metainfo.announce
            self.rerequesters.append(Rerequester(aurl, self.config,
            self.schedule, self.trackers[aurl][0], self._storagewrapper.get_amount_left,
            upmeasure.get_total, downmeasure.get_total, self.reported_port,
            self.infohash, self.finflag, self.internal_shutdown,
            self._announce_done, self.trackers[aurl][1],
            self.trackers[aurl][2]))

        def get_rstats():
            relay_stats = {'relayRate':0, 'relayCount':0, 'relaySent':0}
            for aurl, info in self.trackers.items():
                relay_stats.update(info[0].get_relay_stats())
            return relay_stats

        self._statuscollecter = DownloaderFeedback(choker, upmeasure.get_rate,
            downmeasure.get_rate, upmeasure.get_total, downmeasure.get_total,
            get_rstats, self._ratemeasure.get_time_left,
            self._ratemeasure.get_size_left, self.file_size, self.finflag,
            downloader, self._myfiles)

        self._announced = True

        for req in self.rerequesters:
            req.begin()
        self.started = True
        if not self.finflag.isSet():
            self._activity = ('downloading', 0)
        self.feedback.started(self)

    def got_exception(self, e):
        is_external = False
        if isinstance(e, BTShutdown):
            log.error(str(e))
            is_external = True
        elif isinstance(e, BTFailure):
            log.critical(str(e))
            self._activity = ('download failed: ' + str(e), 0)
        elif isinstance(e, IOError):
            log.critical('IO Error ' + str(e))
            self._activity = ('killed by IO error: ' + str(e), 0)
        elif isinstance(e, OSError):
            log.critical('OS Error ' + str(e))
            self._activity = ('killed by OS error: ' + str(e), 0)
        else:
            data = StringIO()
            print_exc(file=data)
            log.critical(data.getvalue())
            self._activity = ('killed by internal exception: ' + str(e), 0)
        try:
            self._close()
        except Exception, e:
            log.error('Additional error when closing down due to '
                        'error: ' + str(e))
        if is_external:
            self.feedback.failed(self, True)
            return
        if self.config['data_dir'] and self._storage is not None:
            filename = os.path.join(self.config['data_dir'], 'resume',
                                    self.infohash.encode('hex'))
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except Exception, e:
                    log.warning('Could not remove fastresume file '
                                'after failure:' + str(e))
        self.feedback.failed(self, False)

    def _finished(self):
        self.finflag.set()
        # Call self._storage.close() to flush buffers and change files to
        # read-only mode (when they're possibly reopened). Let exceptions
        # from self._storage.close() kill the torrent since files might not
        # be correct on disk if file.close() failed.
        self._storage.close()
        # If we haven't announced yet, normal first announce done later will
        # tell the tracker about seed status.
        self.is_seed = True
        if self._announced:
            for req in self.rerequesters:
                req.announce_finish()
        self._activity = ('seeding', 1)
        if self.config['check_hashes']:
            self._save_fastresume(True)
        self.feedback.finished(self)

    def _save_fastresume(self, on_finish=False):
        if not on_finish and (self.finflag.isSet() or not self.started):
            return
        if not self.config['data_dir']:
            return
        if on_finish:    # self._ratemeasure might not exist yet
            amount_done = self.file_size
        else:
            amount_done = self.file_size - self._ratemeasure.get_size_left()
        filename = os.path.join(self.config['data_dir'], 'resume',
                                self.infohash.encode('hex'))
        resumefile = None
        try:
            resumefile = file(filename, 'wb')
            self._storage.write_fastresume(resumefile, amount_done)
            self._storagewrapper.write_fastresume(resumefile)
            resumefile.close()
        except Exception, e:
            log.warning('Could not write fastresume data: ' + str(e))
            if resumefile is not None:
                resumefile.close()

    def shutdown(self):
        if self.closed:
            return
        try:
            self._close()
            self._save_fastresume()
            self._activity = ('shut down', 0)
        except Exception, e:
            self.got_exception(e)

    def internal_shutdown(self):
        # This is only called when announce fails with no peers,
        # don't try to announce again telling we're leaving the torrent
        self._announced = False
        self.shutdown()
        self.feedback.failed(self, True)

    def _close(self):
        if self.closed:
            return
        self.closed = True

        # GTK Crash Hack
        import time
        time.sleep(.2)

        self.event_handler.remove_context(self)

        self._doneflag.set()
        log.info("Closing connections, please wait...")
        if self._announced:
            for req in self.rerequesters:
                req.announce_stop()
                req.cleanup()
        if self._hashcheck_thread is not None:
            self._hashcheck_thread.join() # should die soon after doneflag set
        if self._myfiles is not None:
            self._filepool.remove_files(self._myfiles)
        if self._listening:
            for aurl, info in self.trackers.items():
                try:
                    info[0].remove_torrent(self.infohash)
                except KeyError, e:
                    continue
        for port in self.reserved_ports:
            for aurl, info in self.trackers.items():
                try:
                    info[4].release_port(port)
                except KeyError, e:
                    continue
        if self._storage is not None:
            self._storage.close()
        self.schedule(0, gc.collect)

    def get_status(self, spew = False, fileinfo=False):
        if self.started and not self.closed:
            r = self._statuscollecter.get_statistics(spew, fileinfo)
            r['activity'] = self._activity[0]
        else:
            r = dict(zip(('activity', 'fractionDone'), self._activity))
        return r

    def get_total_transfer(self):
        if self._upmeasure is None:
            return (0, 0)
        return (self._upmeasure.get_total(), self._downmeasure.get_total())

    def set_option(self, option, value):
        if self.closed:
            return
        if option not in self.config or self.config[option] == value:
            return
        if option not in 'min_uploads max_uploads max_initiate max_allow_in '\
           'data_dir ip max_upload_rate retaliate_to_garbled_data'.split():
            return
        # max_upload_rate doesn't affect upload rate here, just auto uploads
        self.config[option] = value
        self._set_auto_uploads()

    def change_port(self):
        if not self._listening:
            return
        r = self.config['forwarded_port']
        allports = []
        rs = []
        for aurl, info in self.trackers.items():
            allports.append(info[4].port)
        if r:
            for port in self.reserved_ports:
                for aurl, info in self.trackers.items():
                    try:
                        info[4].release_port(port)
                    except KeyError, e:
                        continue
            del self.reserved_ports[:]
            if self.reported_port == r:
                return
        elif self.reported_port not in allports:
            for aurl, info in self.trackers.items():
                try:
                    tr = info[4].get_port(info[0])
                    self.reserved_ports.append(tr)
                    rs.append(tr)
                except KeyError, e:
                    continue
            r = tr  # Blahhhh XXX Richard fix this later after you test!
        else:
            return
        self.reported_port = r
        for aurl, info in self.trackers.items():
            info[4].change_port(r)

    def _announce_done(self):
        for port in self.reserved_ports[:-1]:
            for aurl, info in self.trackers.items():
                try:
                    info[4].release_port(port)
                except KeyError, e:
                    continue
        del self.reserved_ports[:-1]

    def _set_auto_uploads(self):
        uploads = self.config['max_uploads']
        rate = self.config['max_upload_rate']
        if uploads > 0:
            pass
        elif rate <= 0:
            uploads = 7 # unlimited, just guess something here...
        elif rate < 9:
            uploads = 2
        elif rate < 15:
            uploads = 3
        elif rate < 42:
            uploads = 4
        else:
            uploads = int(sqrt(rate * .6))
        self.config['max_uploads_internal'] = uploads

    def rerequest(self):
        for req in self.rerequesters:
            req._announce()

    def scrape(self):
        for req in self.rerequesters:
            # When trackers are up, make this cumulative:
            return req.scrape()

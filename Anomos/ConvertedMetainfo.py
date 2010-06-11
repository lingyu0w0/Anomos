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

# Written by Uoti Urpala

import os
import sys
import hashlib

from Anomos.bencode import bencode
from Anomos import btformats, BTFailure, LOG as log

WINDOWS_UNSUPPORTED_CHARS ='"*/:<>?\|'
windows_translate = [chr(i) for i in range(256)]
for x in WINDOWS_UNSUPPORTED_CHARS:
    windows_translate[ord(x)] = '-'
windows_translate = ''.join(windows_translate)

noncharacter_translate = {}
for i in range(0xD800, 0xE000):
    noncharacter_translate[i] = ord('-')
for i in range(0xFDD0, 0xFDF0):
    noncharacter_translate[i] = ord('-')
for i in (0xFFFE, 0xFFFF):
    noncharacter_translate[i] = ord('-')

del x, i

def set_filesystem_encoding(encoding):
    global filesystem_encoding
    filesystem_encoding = 'ascii'
    if encoding == '':
        try:
            sys.getfilesystemencoding
        except AttributeError:
            log.warning("This seems to be an old Python version which does not support detecting the filesystem encoding. Assuming 'ascii'.")
            return
        encoding = sys.getfilesystemencoding()
        if encoding is None:
            log.warning("Python failed to autodetect filesystem encoding. Using 'ascii' instead.")
            return
    try:
        'a1'.decode(encoding)
    except:
        log.error("Filesystem encoding '"+encoding+"' is not supported. Using 'ascii' instead.")
        return
    filesystem_encoding = encoding


def generate_names(name, is_dir):
    if is_dir:
        prefix = name + '.'
        suffix = ''
    else:
        pos = name.rfind('.')
        if pos == -1:
            pos = len(name)
        prefix = name[:pos] + '.'
        suffix = name[pos:]
    i = 0
    while True:
        yield prefix + str(i) + suffix
        i += 1


class ConvertedMetainfo(object):

    def __init__(self, metainfo):
        self.bad_torrent_wrongfield = False
        self.bad_torrent_unsolvable = False
        self.bad_torrent_noncharacter = False
        self.bad_conversion = False
        self.bad_windows = False
        self.bad_path = False
        self.reported_errors = False
        self.is_batch = False
        self.orig_files = None
        self.files_fs = None
        self.file_size = 0
        self.sizes = []
        self.anon = None

        if metainfo.has_key('anon'):
            self.anon = bool(metainfo['anon'])

        btformats.check_message(metainfo, check_paths=False)
        info = metainfo['info']
        if info.has_key('length'):
            self.file_size = info['length']
            self.sizes.append(self.file_size)
        else:
            self.is_batch = True
            r = []
            self.orig_files = []
            self.sizes = []
            i = 0
            for f in info['files']:
                l = f['length']
                self.file_size += l
                self.sizes.append(l)
                path = self._get_attr_utf8(f, 'path')
                for x in path:
                    if not btformats.allowed_path_re.match(x):
                        if l > 0:
                            raise BTFailure("Bad file path component: "+x)
                        # BitComet makes bad .torrent files with empty
                        # filename part
                        self.bad_path = True
                        break
                else:
                    path = [(self._enforce_utf8(x), x) for x in path]
                    self.orig_files.append('/'.join([x[0] for x in path]))
                    r.append(([(self._to_fs_2(u), u, o) for u, o in path], i))
                    i += 1
            # If two or more file/subdirectory names in the same directory
            # would map to the same name after encoding conversions + Windows
            # workarounds, change them. Files are changed as
            # 'a.b.c'->'a.b.0.c', 'a.b.1.c' etc, directories or files without
            # '.' as 'a'->'a.0', 'a.1' etc. If one of the multiple original
            # names was a "clean" conversion, that one is always unchanged
            # and the rest are adjusted.
            r.sort()
            self.files_fs = [None] * len(r)
            prev = [None]
            res = []
            stack = [{}]
            for x in r:
                j = 0
                x, i = x
                while x[j] == prev[j]:
                    j += 1
                del res[j:]
                del stack[j+1:]
                name = x[j][0][1]
                if name in stack[-1]:
                    for name in generate_names(x[j][1], j != len(x) - 1):
                        name = self._to_fs(name)
                        if name not in stack[-1]:
                            break
                stack[-1][name] = None
                res.append(name)
                for j in range(j + 1, len(x)):
                    name = x[j][0][1]
                    stack.append({name: None})
                    res.append(name)
                self.files_fs[i] = os.path.join(*res)
                prev = x

        self.name = self._get_field_utf8(info, 'name')
        self.name_fs = self._to_fs(self.name)
        self.piece_length = info['piece length']
        if self.piece_length not in [2**18, 2**19, 2**20]:
            raise BTFailure("Torrent uses invalid piece size: %s " %
                    self.piece_length)
        self.announce = metainfo['announce']
        self.hashes = [info['pieces'][x:x+20] for x in xrange(0,
            len(info['pieces']), 20)]
        self.infohash = hashlib.sha1(bencode(info)).digest()
        if metainfo.has_key('announce-list'):
            self.announce_list = metainfo['announce-list']

    def is_anon(self):
        return self.anon

    def show_encoding_errors(self, errorfunc):
        self.reported_errors = True
        if self.bad_torrent_unsolvable:
            errorfunc("error", "This .atorrent file has been created with a broken tool and has incorrectly encoded filenames. Some or all of the filenames may appear different from what the creator of the .atorrent file intended.")
        elif self.bad_torrent_noncharacter:
            errorfunc("error", "This .atorrent file has been created with a broken tool and has bad character values that do not correspond to any real character. Some or all of the filenames may appear different from what the creator of the .atorrent file intended.")
        elif self.bad_torrent_wrongfield:
            errorfunc("error", "This .atorrent file has been created with a broken tool and has incorrectly encoded filenames. The names used may still be correct.")
        elif self.bad_conversion:
            errorfunc("warning", 'The character set used on the local filesystem ("'+filesystem_encoding+'") cannot represent all characters used in the filename(s) of this torrent. Filenames have been changed from the original.')
        elif self.bad_windows:
            errorfunc("warning", 'The Windows filesystem cannot handle some characters used in the filename(s) of this torrent. Filenames have been changed from the original.')
        elif self.bad_path:
            errorfunc("warning", "This .atorrent file has been created with a broken tool and has at least 1 file with an invalid file or directory name. However since all such files were marked as having length 0 those files are just ignored.")

    # At least BitComet seems to make bad .torrent files that have
    # fields in an arbitrary encoding but separate 'field.utf-8' attributes
    def _get_attr_utf8(self, d, attrib):
        v = d.get(attrib + '.utf-8')
        if v is not None:
            if v != d[attrib]:
                self.bad_torrent_wrongfield = True
        else:
            v = d[attrib]
        return v

    def _enforce_utf8(self, s):
        try:
            s = s.decode('utf-8')
        except:
            self.bad_torrent_unsolvable = True
            s = s.decode('utf-8', 'replace')
        t = s.translate(noncharacter_translate)
        if t != s:
            self.bad_torrent_noncharacter = True
        return t.encode('utf-8')

    def _get_field_utf8(self, d, attrib):
        r = self._get_attr_utf8(d, attrib)
        return self._enforce_utf8(r)

    def _fix_windows(self, name, t=windows_translate):
        bad = False
        r = name.translate(t)
        # for some reason name cannot end with '.' or space
        if r[-1] in '. ':
            r = r + '-'
        if r != name:
            self.bad_windows = True
            bad = True
        return (r, bad)

    def _to_fs(self, name):
        return self._to_fs_2(name)[1]

    def _to_fs_2(self, name):
        bad = False
        if sys.platform == 'win32':
            name, bad = self._fix_windows(name)
        name = name.decode('utf-8')
        try:
            r = name.encode(filesystem_encoding)
        except:
            self.bad_conversion = True
            bad = True
            r = name.encode(filesystem_encoding, 'replace')
            # 'replace' could possibly make the name unsupported by windows
            # again, but I think this shouldn't happen with the 'mbcs'
            # encoding. Could happen under Python 2.2 or if someone explicitly
            # specifies a stupid encoding...
        return (bad, r)

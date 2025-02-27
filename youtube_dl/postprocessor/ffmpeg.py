from __future__ import unicode_literals

import io
import os
import subprocess
import time
import re
import collections
from string import ascii_letters
import random
from ..utils import expand_path
# from youtube_dl import YoutubeDL
# todo fail to import youtubedl class for _NUMERIC_FIELDS
# http://python-notes.curiousefficiency.org/en/latest/python_concepts/import_traps.html
_NUMERIC_FIELDS = set((
    'width', 'height', 'tbr', 'abr', 'asr', 'vbr', 'fps', 'filesize', 'filesize_approx',
    'timestamp', 'upload_year', 'upload_month', 'upload_day',
    'duration', 'view_count', 'like_count', 'dislike_count', 'repost_count',
    'average_rating', 'comment_count', 'age_limit',
    'start_time', 'end_time',
    'chapter_number', 'season_number', 'episode_number',
    'track_number', 'disc_number', 'release_year',
    'playlist_index',
))

from .common import AudioConversionError, PostProcessor

from ..compat import (
    compat_subprocess_get_DEVNULL,
)
from ..utils import (
    encodeArgument,
    encodeFilename,
    get_exe_version,
    is_outdated_version,
    PostProcessingError,
    prepend_extension,
    shell_quote,
    subtitles_filename,
    dfxp2srt,
    ISO639Utils,
    replace_extension,
)


EXT_TO_OUT_FORMATS = {
    'aac': 'adts',
    'flac': 'flac',
    'm4a': 'ipod',
    'mka': 'matroska',
    'mkv': 'matroska',
    'mpg': 'mpeg',
    'ogv': 'ogg',
    'ts': 'mpegts',
    'wma': 'asf',
    'wmv': 'asf',
}
ACODECS = {
    'mp3': 'libmp3lame',
    'aac': 'aac',
    'flac': 'flac',
    'm4a': 'aac',
    'opus': 'libopus',
    'vorbis': 'libvorbis',
    'wav': None,
}


class FFmpegPostProcessorError(PostProcessingError):
    pass


class FFmpegPostProcessor(PostProcessor):
    def __init__(self, downloader=None):
        PostProcessor.__init__(self, downloader)
        self._determine_executables()

    def check_version(self):
        if not self.available:
            raise FFmpegPostProcessorError('ffmpeg or avconv not found. Please install one.')

        required_version = '10-0' if self.basename == 'avconv' else '1.0'
        if is_outdated_version(
                self._versions[self.basename], required_version):
            warning = 'Your copy of %s is outdated, update %s to version %s or newer if you encounter any errors.' % (
                self.basename, self.basename, required_version)
            if self._downloader:
                self._downloader.report_warning(warning)

    @staticmethod
    def get_versions(downloader=None):
        return FFmpegPostProcessor(downloader)._versions

    def _determine_executables(self):
        programs = ['avprobe', 'avconv', 'ffmpeg', 'ffprobe']
        prefer_ffmpeg = True

        self.basename = None
        self.probe_basename = None

        self._paths = None
        self._versions = None
        if self._downloader:
            prefer_ffmpeg = self._downloader.params.get('prefer_ffmpeg', True)
            location = self._downloader.params.get('ffmpeg_location')
            if location is not None:
                if not os.path.exists(location):
                    self._downloader.report_warning(
                        'ffmpeg-location %s does not exist! '
                        'Continuing without avconv/ffmpeg.' % (location))
                    self._versions = {}
                    return
                elif not os.path.isdir(location):
                    basename = os.path.splitext(os.path.basename(location))[0]
                    if basename not in programs:
                        self._downloader.report_warning(
                            'Cannot identify executable %s, its basename should be one of %s. '
                            'Continuing without avconv/ffmpeg.' %
                            (location, ', '.join(programs)))
                        self._versions = {}
                        return None
                    location = os.path.dirname(os.path.abspath(location))
                    if basename in ('ffmpeg', 'ffprobe'):
                        prefer_ffmpeg = True

                self._paths = dict(
                    (p, os.path.join(location, p)) for p in programs)
                self._versions = dict(
                    (p, get_exe_version(self._paths[p], args=['-version']))
                    for p in programs)
        if self._versions is None:
            self._versions = dict(
                (p, get_exe_version(p, args=['-version'])) for p in programs)
            self._paths = dict((p, p) for p in programs)

        if prefer_ffmpeg is False:
            prefs = ('avconv', 'ffmpeg')
        else:
            prefs = ('ffmpeg', 'avconv')
        for p in prefs:
            if self._versions[p]:
                self.basename = p
                break

        if prefer_ffmpeg is False:
            prefs = ('avprobe', 'ffprobe')
        else:
            prefs = ('ffprobe', 'avprobe')
        for p in prefs:
            if self._versions[p]:
                self.probe_basename = p
                break

    @property
    def available(self):
        return self.basename is not None

    @property
    def executable(self):
        return self._paths[self.basename]

    @property
    def probe_available(self):
        return self.probe_basename is not None

    @property
    def probe_executable(self):
        return self._paths[self.probe_basename]

    def get_audio_codec(self, path):
        if not self.probe_available:
            raise PostProcessingError('ffprobe or avprobe not found. Please install one.')
        try:
            cmd = [
                encodeFilename(self.probe_executable, True),
                encodeArgument('-show_streams'),
                encodeFilename(self._ffmpeg_filename_argument(path), True)]
            if self._downloader.params.get('verbose', False):
                self._downloader.to_screen('[debug] %s command line: %s' % (self.basename, shell_quote(cmd)))
            handle = subprocess.Popen(cmd, stderr=compat_subprocess_get_DEVNULL(), stdout=subprocess.PIPE, stdin=subprocess.PIPE)
            output = handle.communicate()[0]
            if handle.wait() != 0:
                return None
        except (IOError, OSError):
            return None
        audio_codec = None
        for line in output.decode('ascii', 'ignore').split('\n'):
            if line.startswith('codec_name='):
                audio_codec = line.split('=')[1].strip()
            elif line.strip() == 'codec_type=audio' and audio_codec is not None:
                return audio_codec
        return None

    def run_ffmpeg_multiple_files(self, input_paths, out_path, opts, info):
        self.check_version()

        oldest_mtime = min(
            os.stat(encodeFilename(path)).st_mtime for path in input_paths)

        opts += map(lambda s: self._resolve_postprocessor_arg_var(s, info), self._configuration_args())

        files_cmd = []
        for path in input_paths:
            files_cmd.extend([
                encodeArgument('-i'),
                encodeFilename(self._ffmpeg_filename_argument(path), True)
            ])
        cmd = ([encodeFilename(self.executable, True), encodeArgument('-y')] +
               files_cmd +
               [encodeArgument(o) for o in opts] +
               [encodeFilename(self._ffmpeg_filename_argument(out_path), True)])

        if self._downloader.params.get('verbose', False):
            self._downloader.to_screen('[debug] ffmpeg command line: %s' % shell_quote(cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            stderr = stderr.decode('utf-8', 'replace')
            msg = stderr.strip().split('\n')[-1]
            raise FFmpegPostProcessorError(msg)
        self.try_utime(out_path, oldest_mtime, oldest_mtime)

    def run_ffmpeg(self, path, out_path, opts, info):
        self.run_ffmpeg_multiple_files([path], out_path, opts, info)

    def _ffmpeg_filename_argument(self, fn):
        # Always use 'file:' because the filename may contain ':' (ffmpeg
        # interprets that as a protocol) or can start with '-' (-- is broken in
        # ffmpeg, see https://ffmpeg.org/trac/ffmpeg/ticket/2127 for details)
        # Also leave '-' intact in order not to break streaming to stdout.
        return 'file:' + fn if fn != '-' else fn

    def _resolve_postprocessor_arg_var(self, arg, info):
        # map(lambda s: s % info, args)
        if "%(" not in arg:
            return arg

        template_dict = dict(info)
        template_dict['epoch'] = int(time.time())
        # autonumber_size = self.params.get('autonumber_size')
        # if autonumber_size is None:
        autonumber_size = 5
        #
        # template_dict['autonumber'] = self.params.get('autonumber_start', 1) - 1 + self._num_downloads
        # if template_dict.get('resolution') is None:
        #     if template_dict.get('width') and template_dict.get('height'):
        #         template_dict['resolution'] = '%dx%d' % (template_dict['width'], template_dict['height'])
        #     elif template_dict.get('height'):
        #         template_dict['resolution'] = '%sp' % template_dict['height']
        #     elif template_dict.get('width'):
        #         template_dict['resolution'] = '%dx?' % template_dict['width']
        # sanitize = lambda k, v: sanitize_filename(
        #     compat_str(v),
        #     restricted=self.params.get('restrictfilenames'),
        #     is_id=(k == 'id' or k.endswith('_id')))
        # template_dict = dict((k, v if isinstance(v, compat_numeric_types) else sanitize(k, v))
        #                      for k, v in template_dict.items()
        #                      if v is not None and not isinstance(v, (list, tuple, dict)))
        template_dict = collections.defaultdict(lambda: 'NA', template_dict)
        outtmpl = arg

        # For fields playlist_index and autonumber convert all occurrences
        # of %(field)s to %(field)0Nd for backward compatibility
        field_size_compat_map = {
            'playlist_index': len(str(template_dict['n_entries'])),
            'autonumber': autonumber_size,
        }
        FIELD_SIZE_COMPAT_RE = r'(?<!%)%\((?P<field>autonumber|playlist_index)\)s'
        mobj = re.search(FIELD_SIZE_COMPAT_RE, outtmpl)
        if mobj:
            outtmpl = re.sub(
                FIELD_SIZE_COMPAT_RE,
                r'%%(\1)0%dd' % field_size_compat_map[mobj.group('field')],
                outtmpl)
        # Missing numeric fields used together with integer presentation types
        # in format specification will break the argument substitution since
        # string 'NA' is returned for missing fields. We will patch output
        # template for missing fields to meet string presentation type.
        for numeric_field in _NUMERIC_FIELDS:
            if numeric_field not in template_dict:
                # As of [1] format syntax is:
                #  %[mapping_key][conversion_flags][minimum_width][.precision][length_modifier]type
                # 1. https://docs.python.org/2/library/stdtypes.html#string-formatting
                FORMAT_RE = r'''(?x)
                    (?<!%)
                    %
                    \({0}\)  # mapping key
                    (?:[#0\-+ ]+)?  # conversion flags (optional)
                    (?:\d+)?  # minimum field width (optional)
                    (?:\.\d+)?  # precision (optional)
                    [hlL]?  # length modifier (optional)
                    [diouxXeEfFgGcrs%]  # conversion type
                '''
                outtmpl = re.sub(
                    FORMAT_RE.format(numeric_field),
                    r'%({0})s'.format(numeric_field), outtmpl)
        # expand_path translates '%%' into '%' and '$$' into '$'
        # correspondingly that is not what we want since we need to keep
        # '%%' intact for template dict substitution step. Working around
        # with boundary-alike separator hack.
        sep = ''.join([random.choice(ascii_letters) for _ in range(32)])
        outtmpl = outtmpl.replace('%%', '%{0}%'.format(sep)).replace('$$', '${0}$'.format(sep))
        # outtmpl should be expand_path'ed before template dict substitution
        # because meta fields may contain env variables we don't want to
        # be expanded. For example, for outtmpl "%(title)s.%(ext)s" and
        # title "Hello $PATH", we don't want `$PATH` to be expanded.
        #     todo need encoding & sanitize_path ?
        return expand_path(outtmpl).replace(sep, '') % template_dict


class FFmpegExtractAudioPP(FFmpegPostProcessor):
    def __init__(self, downloader=None, preferredcodec=None, preferredquality=None, nopostoverwrites=False):
        FFmpegPostProcessor.__init__(self, downloader)
        if preferredcodec is None:
            preferredcodec = 'best'
        self._preferredcodec = preferredcodec
        self._preferredquality = preferredquality
        self._nopostoverwrites = nopostoverwrites

    def run_ffmpeg(self, path, out_path, codec, more_opts, info):
        if codec is None:
            acodec_opts = []
        else:
            acodec_opts = ['-acodec', codec]
        opts = ['-vn'] + acodec_opts + more_opts
        try:
            FFmpegPostProcessor.run_ffmpeg(self, path, out_path, opts, info)
        except FFmpegPostProcessorError as err:
            raise AudioConversionError(err.msg)

    def run(self, information):
        path = information['filepath']

        filecodec = self.get_audio_codec(path)
        if filecodec is None:
            raise PostProcessingError('WARNING: unable to obtain file audio codec with ffprobe')

        more_opts = []
        if self._preferredcodec == 'best' or self._preferredcodec == filecodec or (self._preferredcodec == 'm4a' and filecodec == 'aac'):
            if filecodec == 'aac' and self._preferredcodec in ['m4a', 'best']:
                # Lossless, but in another container
                acodec = 'copy'
                extension = 'm4a'
                more_opts = ['-bsf:a', 'aac_adtstoasc']
            elif filecodec in ['aac', 'flac', 'mp3', 'vorbis', 'opus']:
                # Lossless if possible
                acodec = 'copy'
                extension = filecodec
                if filecodec == 'aac':
                    more_opts = ['-f', 'adts']
                if filecodec == 'vorbis':
                    extension = 'ogg'
            else:
                # MP3 otherwise.
                acodec = 'libmp3lame'
                extension = 'mp3'
                more_opts = []
                if self._preferredquality is not None:
                    if int(self._preferredquality) < 10:
                        more_opts += ['-q:a', self._preferredquality]
                    else:
                        more_opts += ['-b:a', self._preferredquality + 'k']
        else:
            # We convert the audio (lossy if codec is lossy)
            acodec = ACODECS[self._preferredcodec]
            extension = self._preferredcodec
            more_opts = []
            if self._preferredquality is not None:
                # The opus codec doesn't support the -aq option
                if int(self._preferredquality) < 10 and extension != 'opus':
                    more_opts += ['-q:a', self._preferredquality]
                else:
                    more_opts += ['-b:a', self._preferredquality + 'k']
            if self._preferredcodec == 'aac':
                more_opts += ['-f', 'adts']
            if self._preferredcodec == 'm4a':
                more_opts += ['-bsf:a', 'aac_adtstoasc']
            if self._preferredcodec == 'vorbis':
                extension = 'ogg'
            if self._preferredcodec == 'wav':
                extension = 'wav'
                more_opts += ['-f', 'wav']

        prefix, sep, ext = path.rpartition('.')  # not os.path.splitext, since the latter does not work on unicode in all setups
        new_path = prefix + sep + extension

        information['filepath'] = new_path
        information['ext'] = extension

        # If we download foo.mp3 and convert it to... foo.mp3, then don't delete foo.mp3, silly.
        if (new_path == path or
                (self._nopostoverwrites and os.path.exists(encodeFilename(new_path)))):
            self._downloader.to_screen('[ffmpeg] Post-process file %s exists, skipping' % new_path)
            return [], information

        try:
            self._downloader.to_screen('[ffmpeg] Destination: ' + new_path)
            self.run_ffmpeg(path, new_path, acodec, more_opts, information)
        except AudioConversionError as e:
            raise PostProcessingError(
                'audio conversion failed: ' + e.msg)
        except Exception:
            raise PostProcessingError('error running ' + self.basename)

        # Try to update the date time for extracted audio file.
        if information.get('filetime') is not None:
            self.try_utime(
                new_path, time.time(), information['filetime'],
                errnote='Cannot update utime of audio file')

        return [path], information


class FFmpegVideoConvertorPP(FFmpegPostProcessor):
    def __init__(self, downloader=None, preferedformat=None):
        super(FFmpegVideoConvertorPP, self).__init__(downloader)
        self._preferedformat = preferedformat

    def run(self, information):
        path = information['filepath']
        if information['ext'] == self._preferedformat:
            self._downloader.to_screen('[ffmpeg] Not converting video file %s - already is in target format %s' % (path, self._preferedformat))
            return [], information
        options = []
        if self._preferedformat == 'avi':
            options.extend(['-c:v', 'libxvid', '-vtag', 'XVID'])
        prefix, sep, ext = path.rpartition('.')
        outpath = prefix + sep + self._preferedformat
        self._downloader.to_screen('[' + 'ffmpeg' + '] Converting video from %s to %s, Destination: ' % (information['ext'], self._preferedformat) + outpath)
        self.run_ffmpeg(path, outpath, options, information)
        information['filepath'] = outpath
        information['format'] = self._preferedformat
        information['ext'] = self._preferedformat
        return [path], information


class FFmpegEmbedSubtitlePP(FFmpegPostProcessor):
    def run(self, information):
        if information['ext'] not in ('mp4', 'webm', 'mkv'):
            self._downloader.to_screen('[ffmpeg] Subtitles can only be embedded in mp4, webm or mkv files')
            return [], information
        subtitles = information.get('requested_subtitles')
        if not subtitles:
            self._downloader.to_screen('[ffmpeg] There aren\'t any subtitles to embed')
            return [], information

        filename = information['filepath']

        ext = information['ext']
        sub_langs = []
        sub_filenames = []
        webm_vtt_warn = False

        for lang, sub_info in subtitles.items():
            sub_ext = sub_info['ext']
            if ext != 'webm' or ext == 'webm' and sub_ext == 'vtt':
                sub_langs.append(lang)
                sub_filenames.append(subtitles_filename(filename, lang, sub_ext))
            else:
                if not webm_vtt_warn and ext == 'webm' and sub_ext != 'vtt':
                    webm_vtt_warn = True
                    self._downloader.to_screen('[ffmpeg] Only WebVTT subtitles can be embedded in webm files')

        if not sub_langs:
            return [], information

        input_files = [filename] + sub_filenames

        opts = [
            '-map', '0',
            '-c', 'copy',
            # Don't copy the existing subtitles, we may be running the
            # postprocessor a second time
            '-map', '-0:s',
        ]
        if information['ext'] == 'mp4':
            opts += ['-c:s', 'mov_text']
        for (i, lang) in enumerate(sub_langs):
            opts.extend(['-map', '%d:0' % (i + 1)])
            lang_code = ISO639Utils.short2long(lang)
            if lang_code is not None:
                opts.extend(['-metadata:s:s:%d' % i, 'language=%s' % lang_code])

        temp_filename = prepend_extension(filename, 'temp')
        self._downloader.to_screen('[ffmpeg] Embedding subtitles in \'%s\'' % filename)
        self.run_ffmpeg_multiple_files(input_files, temp_filename, opts, information)
        os.remove(encodeFilename(filename))
        os.rename(encodeFilename(temp_filename), encodeFilename(filename))

        return sub_filenames, information


class FFmpegMetadataPP(FFmpegPostProcessor):
    def run(self, info):
        metadata = {}

        def add(meta_list, info_list=None):
            if not info_list:
                info_list = meta_list
            if not isinstance(meta_list, (list, tuple)):
                meta_list = (meta_list,)
            if not isinstance(info_list, (list, tuple)):
                info_list = (info_list,)
            for info_f in info_list:
                if info.get(info_f) is not None:
                    for meta_f in meta_list:
                        metadata[meta_f] = info[info_f]
                    break

        add('title', ('track', 'title'))
        add('date', 'upload_date')
        add(('description', 'comment'), 'description')
        add('purl', 'webpage_url')
        add('track', 'track_number')
        add('artist', ('artist', 'creator', 'uploader', 'uploader_id'))
        add('genre')
        add('album')
        add('album_artist')
        add('disc', 'disc_number')

        if not metadata:
            self._downloader.to_screen('[ffmpeg] There isn\'t any metadata to add')
            return [], info

        filename = info['filepath']
        temp_filename = prepend_extension(filename, 'temp')
        in_filenames = [filename]
        options = []

        if info['ext'] == 'm4a':
            options.extend(['-vn', '-acodec', 'copy'])
        else:
            options.extend(['-c', 'copy'])

        for (name, value) in metadata.items():
            options.extend(['-metadata', '%s=%s' % (name, value)])

        chapters = info.get('chapters', [])
        if chapters:
            metadata_filename = replace_extension(filename, 'meta')
            with io.open(metadata_filename, 'wt', encoding='utf-8') as f:
                def ffmpeg_escape(text):
                    return re.sub(r'(=|;|#|\\|\n)', r'\\\1', text)

                metadata_file_content = ';FFMETADATA1\n'
                for chapter in chapters:
                    metadata_file_content += '[CHAPTER]\nTIMEBASE=1/1000\n'
                    metadata_file_content += 'START=%d\n' % (chapter['start_time'] * 1000)
                    metadata_file_content += 'END=%d\n' % (chapter['end_time'] * 1000)
                    chapter_title = chapter.get('title')
                    if chapter_title:
                        metadata_file_content += 'title=%s\n' % ffmpeg_escape(chapter_title)
                f.write(metadata_file_content)
                in_filenames.append(metadata_filename)
                options.extend(['-map_metadata', '1'])

        self._downloader.to_screen('[ffmpeg] Adding metadata to \'%s\'' % filename)
        self.run_ffmpeg_multiple_files(in_filenames, temp_filename, options, info)
        if chapters:
            os.remove(metadata_filename)
        os.remove(encodeFilename(filename))
        os.rename(encodeFilename(temp_filename), encodeFilename(filename))
        return [], info


class FFmpegMergerPP(FFmpegPostProcessor):
    def run(self, info):
        filename = info['filepath']
        temp_filename = prepend_extension(filename, 'temp')
        args = ['-c', 'copy', '-map', '0:v:0', '-map', '1:a:0']
        self._downloader.to_screen('[ffmpeg] Merging formats into "%s"' % filename)
        self.run_ffmpeg_multiple_files(info['__files_to_merge'], temp_filename, args, info)
        os.rename(encodeFilename(temp_filename), encodeFilename(filename))
        return info['__files_to_merge'], info

    def can_merge(self):
        # TODO: figure out merge-capable ffmpeg version
        if self.basename != 'avconv':
            return True

        required_version = '10-0'
        if is_outdated_version(
                self._versions[self.basename], required_version):
            warning = ('Your copy of %s is outdated and unable to properly mux separate video and audio files, '
                       'youtube-dl will download single file media. '
                       'Update %s to version %s or newer to fix this.') % (
                           self.basename, self.basename, required_version)
            if self._downloader:
                self._downloader.report_warning(warning)
            return False
        return True


class FFmpegFixupStretchedPP(FFmpegPostProcessor):
    def run(self, info):
        stretched_ratio = info.get('stretched_ratio')
        if stretched_ratio is None or stretched_ratio == 1:
            return [], info

        filename = info['filepath']
        temp_filename = prepend_extension(filename, 'temp')

        options = ['-c', 'copy', '-aspect', '%f' % stretched_ratio]
        self._downloader.to_screen('[ffmpeg] Fixing aspect ratio in "%s"' % filename)
        self.run_ffmpeg(filename, temp_filename, options, info)

        os.remove(encodeFilename(filename))
        os.rename(encodeFilename(temp_filename), encodeFilename(filename))

        return [], info


class FFmpegFixupM4aPP(FFmpegPostProcessor):
    def run(self, info):
        if info.get('container') != 'm4a_dash':
            return [], info

        filename = info['filepath']
        temp_filename = prepend_extension(filename, 'temp')

        options = ['-c', 'copy', '-f', 'mp4']
        self._downloader.to_screen('[ffmpeg] Correcting container in "%s"' % filename)
        self.run_ffmpeg(filename, temp_filename, options, info)

        os.remove(encodeFilename(filename))
        os.rename(encodeFilename(temp_filename), encodeFilename(filename))

        return [], info


class FFmpegFixupM3u8PP(FFmpegPostProcessor):
    def run(self, info):
        filename = info['filepath']
        if self.get_audio_codec(filename) == 'aac':
            temp_filename = prepend_extension(filename, 'temp')

            options = ['-c', 'copy', '-f', 'mp4', '-bsf:a', 'aac_adtstoasc']
            self._downloader.to_screen('[ffmpeg] Fixing malformed AAC bitstream in "%s"' % filename)
            self.run_ffmpeg(filename, temp_filename, options, info)

            os.remove(encodeFilename(filename))
            os.rename(encodeFilename(temp_filename), encodeFilename(filename))
        return [], info


class FFmpegSubtitlesConvertorPP(FFmpegPostProcessor):
    def __init__(self, downloader=None, format=None):
        super(FFmpegSubtitlesConvertorPP, self).__init__(downloader)
        self.format = format

    def run(self, info):
        subs = info.get('requested_subtitles')
        filename = info['filepath']
        new_ext = self.format
        new_format = new_ext
        if new_format == 'vtt':
            new_format = 'webvtt'
        if subs is None:
            self._downloader.to_screen('[ffmpeg] There aren\'t any subtitles to convert')
            return [], info
        self._downloader.to_screen('[ffmpeg] Converting subtitles')
        sub_filenames = []
        for lang, sub in subs.items():
            ext = sub['ext']
            if ext == new_ext:
                self._downloader.to_screen(
                    '[ffmpeg] Subtitle file for %s is already in the requested format' % new_ext)
                continue
            old_file = subtitles_filename(filename, lang, ext)
            sub_filenames.append(old_file)
            new_file = subtitles_filename(filename, lang, new_ext)

            if ext in ('dfxp', 'ttml', 'tt'):
                self._downloader.report_warning(
                    'You have requested to convert dfxp (TTML) subtitles into another format, '
                    'which results in style information loss')

                dfxp_file = old_file
                srt_file = subtitles_filename(filename, lang, 'srt')

                with open(dfxp_file, 'rb') as f:
                    srt_data = dfxp2srt(f.read())

                with io.open(srt_file, 'wt', encoding='utf-8') as f:
                    f.write(srt_data)
                old_file = srt_file

                subs[lang] = {
                    'ext': 'srt',
                    'data': srt_data
                }

                if new_ext == 'srt':
                    continue
                else:
                    sub_filenames.append(srt_file)

            self.run_ffmpeg(old_file, new_file, ['-f', new_format], info)

            with io.open(new_file, 'rt', encoding='utf-8') as f:
                subs[lang] = {
                    'ext': new_ext,
                    'data': f.read(),
                }

        return sub_filenames, info

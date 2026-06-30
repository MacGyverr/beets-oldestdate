"""
Patched replacement for kernitus/beets-oldestdate beetsplug/oldestdate.py.

This build keeps the original release-date approach but adds practical safeguards
for large MusicBrainz works and intermittent network failures:
  * visible phase/progress lines during import and date scans;
  * safe handling for blank/zero file dates;
  * bounded socket waits and retries;
  * a per-track time limit that preserves the best oldest date found so far;
  * an append-only skipped-track log at <beets directory>/oldestdate-skipped.txt;
  * user-selected defaults for import-time matching and date overwrite behavior.

It remains compatible with the older one-file beetsplug/oldestdate.py layout.
It does reset the default settings to these, and adds new settings for preventing stalls and better monitoring.

oldestdate:
  auto: yes
  filter_on_import: yes
  ignore_track_id: no
  prompt_missing_work_id: no
  force: yes
  overwrite_date: yes
  overwrite_month: no
  overwrite_day: no
  filter_recordings: yes
  approach: releases
  use_file_date: no
  max_network_retries: 5

  show_progress: no  #yes will show what is going on in the background.
  progress_every: 1  #will show how many lines of what is going on, 1 to show it all
  max_related_recordings: 200 #Skips anything above this for a giant MusicBrainz work instead of crawling it indefinitely
  max_scan_seconds: 120 #Gives each track up to two minutes before skipping it
  request_timeout: 20 #Limits a stalled MusicBrainz socket request to 20 seconds
  network_retries: 2 #Two visible attempts per MusicBrainz request
  minimum_file_year: 1000 #Treats `0`, blank, and absurdly old embedded years as unknown    


"""

import datetime
import os
import socket
import threading
import time

from dateutil import parser

import mediafile
import musicbrainzngs
from beets import config, ui
from beets.autotag import hooks
from beets.importer import action
from beets.plugins import BeetsPlugin


musicbrainzngs.set_useragent(
    "Beets oldestdate plugin",
    "1.1.4-patched3",
    "https://github.com/MacGyverr/beets-oldestdate",
)


class ScanTimedOut(Exception):
    """Raised when one track reaches its scan budget.

    ``partial_date`` is the oldest candidate found before the timeout. It is
    intentionally carried upward so the caller can save useful progress rather
    than discarding it.
    """

    def __init__(self, phase, partial_date=None):
        self.phase = phase
        self.partial_date = partial_date
        super().__init__('scan time limit reached while ' + phase)


class SkipTrack(Exception):
    """A nonfatal per-track failure that should be written to the skip log."""



def _optional_date_component(value, upper_limit):
    """Return an integer component in range, or None for blank/invalid tags."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= upper_limit else None


# MusicBrainz artist-credit arrays can contain both dictionaries and literal
# join phrases, for example: [artist dict, " feat. ", artist dict].
# Never assume every entry is a mapping.
def _mapping(value):
    return value if isinstance(value, dict) else {}


# Extract first valid work_id from recording.
def _get_work_id_from_recording(recording):
    for work_rel in _mapping(recording).get('work-relation-list', []):
        current_work = _mapping(_mapping(work_rel).get('work'))
        if current_work.get('id'):
            return current_work['id']
    return None


# Returns whether this recording contains at least one of the specified artists.
def _contains_artist(recording, artist_ids):
    for credit in _mapping(recording).get('artist-credit', []):
        artist = _mapping(_mapping(credit).get('artist'))
        if artist.get('id') in artist_ids:
            return True
    return False


# Extract artist ids from a recording.
def _get_artist_ids_from_recording(recording):
    ids = []
    for credit in _mapping(recording).get('artist-credit', []):
        artist = _mapping(_mapping(credit).get('artist'))
        if artist.get('id'):
            ids.append(artist['id'])
    return ids


# Returns whether given fetched recording is a cover of a work.
def _is_cover(recording):
    for work in _mapping(recording).get('work-relation-list', []):
        if 'cover' in _mapping(work).get('attribute-list', []):
            return True
    return False


class DateWrapper(datetime.datetime):
    """
    datetime wrapper that permits YYYY, YYYY-MM, and YYYY-MM-DD comparisons.

    A year of 0 is invalid metadata, not "year 1". The original plugin clamped
    zero to Python's earliest supported year, which made missing dates win every
    oldest-date comparison. This version rejects zero instead.
    """

    def __new__(cls, y=None, m=None, d=None, iso_string=None):
        if y is not None:
            try:
                year = int(y)
            except (TypeError, ValueError):
                raise ValueError("Invalid value for year")

            if year < datetime.MINYEAR or year > datetime.MAXYEAR:
                raise ValueError("Invalid value for year")

            month = _optional_date_component(m, 12) or 1
            day = _optional_date_component(d, 31) or 1
            return datetime.datetime.__new__(cls, year, month, day)

        if iso_string is not None:
            parsed = parser.isoparse(iso_string)
            return datetime.datetime.__new__(cls, parsed.year, parsed.month, parsed.day)

        raise TypeError("Must specify a value for year or a date string")

    @classmethod
    def today(cls):
        today = datetime.date.today()
        return DateWrapper(today.year, today.month, today.day)

    def __init__(self, y=None, m=None, d=None, iso_string=None):
        if y is not None:
            self.y = int(y)
            self.m = _optional_date_component(m, 12)
            self.d = _optional_date_component(d, 31)
            return

        if iso_string is not None:
            iso_string = str(iso_string).replace("-", "")
            length = len(iso_string)
            if length < 4:
                raise ValueError("Invalid value for year")

            self.y = int(iso_string[:4])
            if self.y < datetime.MINYEAR or self.y > datetime.MAXYEAR:
                raise ValueError("Invalid value for year")

            self.m = None
            self.d = None
            if length >= 6:
                month = int(iso_string[4:6])
                if month < 1 or month > 12:
                    raise ValueError("Invalid value for month")
                self.m = month
            if length >= 8:
                day = int(iso_string[6:8])
                if day < 1 or day > 31:
                    raise ValueError("Invalid value for day")
                self.d = day
            return

        raise TypeError("Must specify a value for year or a date string")

    def __lt__(self, other):
        if self.y != other.y:
            return self.y < other.y
        if self.m is None:
            return False
        if other.m is None:
            return True
        if self.m != other.m:
            return self.m < other.m
        if self.d is None:
            return False
        if other.d is None:
            return True
        return self.d < other.d

    def __eq__(self, other):
        if self.y != other.y:
            return False
        if self.m is not None and other.m is not None:
            if self.d is not None and other.d is not None:
                return self.m == other.m and self.d == other.d
            return self.m == other.m
        return self.m == other.m


class OldestDatePlugin(BeetsPlugin):
    _importing = False
    _recordings_cache = dict()

    def __init__(self):
        super(OldestDatePlugin, self).__init__()
        self.import_stages = [self._on_import]
        self._recordings_cache = {}
        self._skip_log_lock = threading.Lock()
        self.config.add({
            # Matching and write defaults.
            'auto': True,
            'filter_on_import': True,
            'ignore_track_id': False,
            'prompt_missing_work_id': False,
            'force': True,
            'overwrite_date': True,
            # These follow the upstream plugin semantics: False clears the
            # corresponding embedded field instead of copying an uncertain
            # month/day from a later compilation or remaster.
            'overwrite_month': False,
            'overwrite_day': False,
            'filter_recordings': True,
            'approach': 'releases',  # recordings, releases, hybrid, both
            'release_types': None,
            'use_file_date': False,

            # Network and scan safety defaults.
            # Low-level python-musicbrainzngs attempts per HTTP request.
            'max_network_retries': 5,
            # Visible outer attempts made by this patch around a MusicBrainz
            # call. Retained from the earlier patch for compatibility.
            'network_retries': 2,
            'retry_delay_seconds': 2,
            'request_timeout': 20,
            # 0 disables the corresponding limit.
            'max_related_recordings': 200,
            'max_scan_seconds': 120,

            # Progress and metadata cleanup defaults.
            'show_progress': False,
            'progress_every': 1,
            'minimum_file_year': 1000,
        })

        self._install_musicbrainz_network_limits()

        if self.config['auto']:
            if self.config['ignore_track_id']:
                self.register_listener('import_task_created', self._import_task_created)
            if self.config['prompt_missing_work_id']:
                self.register_listener('import_task_choice', self._import_task_choice)
            if self.config['filter_on_import']:
                self.register_listener('trackinfo_received', self._import_trackinfo)
                config['match']['distance_weights'].add({'work_id': 4})

        musicbrainzngs.set_hostname(config['musicbrainz']['host'].get())
        musicbrainzngs.set_rate_limit(1, config['musicbrainz']['ratelimit'].get())

        for recording_field in ('recording_year', 'recording_month', 'recording_day'):
            field = mediafile.MediaField(
                mediafile.MP3DescStorageStyle(recording_field),
                mediafile.MP4StorageStyle('----:com.apple.iTunes:{}'.format(recording_field)),
                mediafile.StorageStyle(recording_field),
            )
            self.add_media_field(recording_field, field)

    def commands(self):
        recording_date_command = ui.Subcommand(
            'oldestdate',
            help="Retrieve the date of the oldest known recording or release of a track.",
            aliases=['olddate'],
        )
        recording_date_command.func = self._command_func
        return [recording_date_command]

    # ------------------------------------------------------------------
    # Console status, limits, and MusicBrainz request handling.
    # ------------------------------------------------------------------

    def _status(self, message):
        """Print a normal console line; do not require Beets verbose mode."""
        self._log.info(message)
        if self.config['show_progress'].get():
            ui.print_(u'[oldestdate] ' + message)

    def _install_musicbrainz_network_limits(self):
        """
        Apply a socket timeout and set python-musicbrainzngs' low-level retry
        count from ``max_network_retries``. The visible outer retry loop remains
        separate so the importer can show where it is waiting.
        """
        timeout = float(self.config['request_timeout'].get())
        if timeout > 0:
            socket.setdefaulttimeout(timeout)

        try:
            import musicbrainzngs.musicbrainz as mb_http
        except ImportError:
            self._log.warning('Could not load musicbrainzngs HTTP internals; request retry limit was not patched.')
            return

        current_safe_read = getattr(mb_http, '_safe_read', None)
        if current_safe_read is None:
            self._log.warning('musicbrainzngs has no _safe_read function; request retry limit was not patched.')
            return

        # Do not stack wrappers if Beets reloads the plugin in-process.
        if getattr(current_safe_read, '_oldestdate_patched', False):
            return

        original_safe_read = current_safe_read
        plugin = self

        def limited_safe_read(opener, req, body=None, max_retries=None, retry_delay_delta=2.0):
            # Preserve the upstream oldestdate option. This governs retries
            # within a single HTTP call; the separate ``network_retries``
            # option controls the visible outer retry loop below.
            configured_retries = max(
                1,
                int(plugin.config['max_network_retries'].get()),
            )
            return original_safe_read(
                opener,
                req,
                body,
                max_retries=configured_retries,
                retry_delay_delta=1.0,
            )

        limited_safe_read._oldestdate_patched = True
        mb_http._safe_read = limited_safe_read

    def _deadline_for_current_track(self):
        seconds = float(self.config['max_scan_seconds'].get())
        if seconds <= 0:
            return None
        return time.monotonic() + seconds

    @staticmethod
    def _format_date(date_value):
        if not date_value:
            return 'none'
        if date_value.d is not None:
            return '{:04d}-{:02d}-{:02d}'.format(date_value.y, date_value.m, date_value.d)
        if date_value.m is not None:
            return '{:04d}-{:02d}'.format(date_value.y, date_value.m)
        return '{:04d}'.format(date_value.y)

    def _check_deadline(self, deadline, phase):
        if deadline is not None and time.monotonic() >= deadline:
            raise ScanTimedOut(phase)

    def _musicbrainz_call(self, label, func, deadline, *args, **kwargs):
        """Run a MusicBrainz call with visible status and bounded retries."""
        attempts = max(1, int(self.config['network_retries'].get()))
        retry_delay = max(0.0, float(self.config['retry_delay_seconds'].get()))
        last_error = None

        for attempt in range(1, attempts + 1):
            self._check_deadline(deadline, label)
            if attempt == 1:
                self._status(label)
            else:
                self._status('{} (retry {}/{})'.format(label, attempt, attempts))
            try:
                return func(*args, **kwargs)
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._status('{} failed: {}. Waiting {:.0f}s before retry.'.format(
                    label, exc, retry_delay))
                if retry_delay:
                    time.sleep(retry_delay)

        raise last_error

    def _item_date_or_none(self, item):
        """Return an existing valid file date, or None for blank/zero dates."""
        try:
            year = int(item.year)
        except (TypeError, ValueError):
            return None

        minimum = int(self.config['minimum_file_year'].get())
        if year < minimum:
            return None

        try:
            return DateWrapper(year, item.month, item.day)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Import hooks.
    # ------------------------------------------------------------------

    def _import_trackinfo(self, info):
        # Fetch the recording associated with each candidate only when the
        # optional candidate filter is explicitly enabled.
        if 'track_id' in info:
            try:
                self._fetch_recording(info.track_id, None, 'candidate recording', quiet=True)
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError, ScanTimedOut) as exc:
                self._log.debug('Candidate recording fetch failed: {0}', exc)

    def track_distance(self, session, info):
        dist = hooks.Distance()
        if self.config['filter_on_import']:
            try:
                if not self._has_work_id(info.track_id):
                    dist.add('work_id', 1)
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError, ScanTimedOut):
                # Do not make a transient network error block the importer.
                dist.add('work_id', 1)
        return dist

    def _import_task_created(self, task, session):
        task.item.mb_trackid = None

    def _import_task_choice(self, task, session):
        match = task.match
        if not match:
            return
        match = match.info

        recording_id = match.track_id
        search_link = (
            'https://musicbrainz.org/search?query=' + match.title.replace(' ', '+')
            + '+artist%3A%22' + match.artist.replace(' ', '+')
            + '%22&type=recording&limit=100&method=advanced'
        )

        while True:
            try:
                has_work = self._has_work_id(recording_id)
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError) as exc:
                self._status('Cannot check MusicBrainz work link for {} - {}: {}'.format(
                    match.artist, match.title, exc))
                has_work = False

            if has_work:
                return

            try:
                recording_date = self._get_oldest_date(
                    recording_id,
                    self._item_date_or_none(task.item),
                )
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError, ScanTimedOut) as exc:
                recording_date = None
                self._status('Could not inspect no-work-ID recording: {}'.format(exc))

            recording_year_string = (
                None if recording_date is None else self._format_date(recording_date)
            )
            self._log.error(
                '{0.artist} - {0.title} ({1}) has no associated work! Please fix and try again!',
                match,
                recording_year_string,
            )
            ui.print_('Search link: ' + search_link)
            sel = ui.input_options(('Use this recording', 'Try again', 'Skip track'))

            if sel == 't':
                self._recordings_cache.pop(recording_id, None)
                try:
                    self._fetch_recording(recording_id, None, 'selected recording', quiet=False)
                except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError) as exc:
                    self._status('Refresh failed: {}'.format(exc))
            elif sel == 'u':
                return
            else:
                task.choice_flag = action.SKIP
                return

    # ------------------------------------------------------------------
    # Primary command/import processing.
    # ------------------------------------------------------------------

    def _command_func(self, lib, session, args):
        for item in lib.items(args):
            self._process_file(item)

    def _on_import(self, session, task):
        if self.config['auto']:
            self._importing = True
            for item in task.imported_items():
                self._process_file(item)

    def _skip_log_path(self):
        """Return <beets directory>/oldestdate-skipped.txt."""
        directory = config['directory'].get()
        if not directory:
            return None
        return os.path.join(
            os.path.abspath(os.path.expanduser(os.fsdecode(directory))),
            'oldestdate-skipped.txt',
        )

    def _log_skipped(self, item, reason):
        """Append a failed track and reason without letting logging break import."""
        log_path = self._skip_log_path()
        if not log_path:
            self._log.warning('No Beets directory is configured; skipped-track log was not written.')
            return

        try:
            path = os.fsdecode(item.path) if getattr(item, 'path', None) else ''
            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = '{} | {} - {} | {} | {}\n'.format(
                timestamp,
                item.artist,
                item.title,
                path,
                reason,
            )
            with self._skip_log_lock:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as handle:
                    handle.write(line)
        except OSError as exc:
            self._log.error('Could not append to skipped-track log {}: {}', log_path, exc)

    def _skip_item(self, item, reason):
        label = '{} - {}'.format(item.artist, item.title)
        self._status('Skipping {}: {}.'.format(label, reason))
        self._log_skipped(item, reason)

    @staticmethod
    def _has_usable_date(date_value):
        return date_value is not None and date_value != DateWrapper.today()

    def _apply_date(self, item, oldest_date, timed_out=False):
        """Store recording_* values and write the selected date fields."""
        if oldest_date.y is not None:
            item['recording_year'] = oldest_date.y
        if oldest_date.m is not None:
            item['recording_month'] = oldest_date.m
        if oldest_date.d is not None:
            item['recording_day'] = oldest_date.d

        if self.config['overwrite_date']:
            self._log.warning(
                'Overwriting date field for: {0.artist} - {0.title} from '
                '{0.year}-{0.month}-{0.day} to {1}',
                item,
                self._format_date(oldest_date),
            )
            item.year = str(oldest_date.y).zfill(4)
            item.month = (
                str(oldest_date.m).zfill(2)
                if self.config['overwrite_month'] and oldest_date.m is not None
                else ''
            )
            item.day = (
                str(oldest_date.d).zfill(2)
                if self.config['overwrite_day'] and oldest_date.d is not None
                else ''
            )

        self._log.info('Applying changes to {0.artist} - {0.title}', item)
        item.store()
        if not self._importing:
            item.write()

        suffix = ' (best result before timeout)' if timed_out else ''
        self._status('Finished: {} - {} -> {}{}.'.format(
            item.artist,
            item.title,
            self._format_date(oldest_date),
            suffix,
        ))

    def _process_file(self, item):
        label = '{} - {}'.format(item.artist, item.title)

        if not item.mb_trackid:
            self._skip_item(item, 'no MusicBrainz recording ID')
            return

        if 'recording_year' in item and item.recording_year and not self.config['force']:
            self._status('Skipping {}: already has recording_year {}.'.format(
                label, item.recording_year))
            return

        started = time.monotonic()
        deadline = self._deadline_for_current_track()
        self._status('Starting: {}'.format(label))

        try:
            oldest_date = self._get_oldest_date(
                item.mb_trackid,
                self._item_date_or_none(item),
                deadline,
            )
        except ScanTimedOut as exc:
            elapsed = time.monotonic() - started
            partial_date = exc.partial_date
            if self._has_usable_date(partial_date):
                self._status(
                    'Time limit reached for {} after {:.1f}s while {}; '
                    'using oldest result found so far: {}.'.format(
                        label,
                        elapsed,
                        exc.phase,
                        self._format_date(partial_date),
                    )
                )
                self._apply_date(item, partial_date, timed_out=True)
            else:
                self._skip_item(
                    item,
                    'scan timed out after {:.1f}s while {} before a usable date was found'.format(
                        elapsed,
                        exc.phase,
                    ),
                )
            return
        except SkipTrack as exc:
            self._skip_item(item, str(exc))
            return
        except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError) as exc:
            self._skip_item(item, 'MusicBrainz request failed: {}'.format(exc))
            return
        except Exception as exc:
            # Keep a malformed MusicBrainz response or a future plugin edge
            # case from killing the whole Beets import. KeyboardInterrupt and
            # SystemExit are intentionally not caught here.
            self._skip_item(item, '{}: {}'.format(type(exc).__name__, exc))
            return

        if not self._has_usable_date(oldest_date):
            self._skip_item(item, 'no usable oldest release date found')
            return

        self._apply_date(item, oldest_date)

    # ------------------------------------------------------------------
    # MusicBrainz data retrieval.
    # ------------------------------------------------------------------

    def _fetch_recording(self, recording_id, deadline, phase, quiet=False):
        if recording_id in self._recordings_cache:
            return self._recordings_cache[recording_id]

        label = 'Fetching {} {}'.format(phase, recording_id)
        if quiet:
            # Candidate lookups can be numerous. Leave them in Beets logs only.
            recording = self._musicbrainz_call_quiet(
                label,
                musicbrainzngs.get_recording_by_id,
                deadline,
                recording_id,
                ['artists', 'releases', 'work-rels'],
            )['recording']
        else:
            recording = self._musicbrainz_call(
                label,
                musicbrainzngs.get_recording_by_id,
                deadline,
                recording_id,
                ['artists', 'releases', 'work-rels'],
            )['recording']

        self._recordings_cache[recording_id] = recording
        return recording

    def _musicbrainz_call_quiet(self, label, func, deadline, *args, **kwargs):
        """Same limits as _musicbrainz_call but logs rather than prints."""
        attempts = max(1, int(self.config['network_retries'].get()))
        retry_delay = max(0.0, float(self.config['retry_delay_seconds'].get()))
        last_error = None

        for attempt in range(1, attempts + 1):
            self._check_deadline(deadline, label)
            self._log.debug('{} (attempt {}/{})'.format(label, attempt, attempts))
            try:
                return func(*args, **kwargs)
            except (musicbrainzngs.NetworkError, musicbrainzngs.ResponseError) as exc:
                last_error = exc
                if attempt < attempts and retry_delay:
                    time.sleep(retry_delay)

        raise last_error

    def _get_recording(self, recording_id, deadline, phase='recording', quiet=False):
        if recording_id in self._recordings_cache:
            return self._recordings_cache[recording_id]
        return self._fetch_recording(recording_id, deadline, phase, quiet)

    def _fetch_work(self, work_id, deadline):
        return self._musicbrainz_call(
            'Fetching MusicBrainz work {}'.format(work_id),
            musicbrainzngs.get_work_by_id,
            deadline,
            work_id,
            ['recording-rels'],
        )['work']

    def _has_work_id(self, recording_id):
        recording = self._get_recording(recording_id, None, 'candidate recording', quiet=True)
        return _get_work_id_from_recording(recording) is not None

    # ------------------------------------------------------------------
    # Date search.
    # ------------------------------------------------------------------

    def _raise_timeout_with_partial(self, timeout, oldest_date):
        """Preserve the best candidate when a nested scan reaches its deadline."""
        partial = timeout.partial_date
        if partial is None:
            partial = oldest_date
        raise ScanTimedOut(timeout.phase, partial)

    def _extract_oldest_recording_date(
            self, recordings, starting_date, is_cover, approach, deadline):
        oldest_date = starting_date
        total = len(recordings)
        every = max(1, int(self.config['progress_every'].get()))

        self._status('Checking recording begin dates for {} linked recordings.'.format(total))
        for index, raw_rec in enumerate(recordings, 1):
            try:
                self._check_deadline(deadline, 'checking recording dates')
            except ScanTimedOut as exc:
                self._raise_timeout_with_partial(exc, oldest_date)

            if index == 1 or index == total or index % every == 0:
                self._status('Recording-date scan: {}/{}; current oldest {}.'.format(
                    index, total, self._format_date(oldest_date)))

            rec = _mapping(raw_rec)
            rec_obj = _mapping(rec.get('recording'))
            if not rec_obj.get('id'):
                continue
            rec_id = rec_obj['id']

            rec_is_cover = 'cover' in rec.get('attribute-list', [])
            if is_cover != rec_is_cover:
                self._recordings_cache.pop(rec_id, None)
                continue

            date_string = rec.get('begin')
            if date_string:
                try:
                    date = DateWrapper(iso_string=date_string)
                    if date < oldest_date:
                        oldest_date = date
                except ValueError:
                    self._log.error('Could not parse date {0} for recording {1}', date_string, rec)

            if approach == 'recordings' or (
                    approach == 'hybrid' and oldest_date != starting_date):
                self._recordings_cache.pop(rec_id, None)

        return oldest_date

    def _extract_oldest_release_date(
            self, recordings, starting_date, is_cover, artist_ids, deadline):
        oldest_date = starting_date
        release_types = self.config['release_types'].get()
        total = len(recordings)
        every = max(1, int(self.config['progress_every'].get()))

        self._status('Scanning release dates for {} linked recordings.'.format(total))
        for index, raw_rec in enumerate(recordings, 1):
            try:
                self._check_deadline(deadline, 'scanning release dates')
            except ScanTimedOut as exc:
                self._raise_timeout_with_partial(exc, oldest_date)

            if index == 1 or index == total or index % every == 0:
                self._status('Release-date scan: {}/{}; current oldest {}.'.format(
                    index, total, self._format_date(oldest_date)))

            rec = _mapping(raw_rec)
            rec_obj = _mapping(rec.get('recording', rec))
            if not rec_obj.get('id'):
                continue
            rec_id = rec_obj['id']
            fetched_recording = None

            if is_cover:
                if 'cover' not in rec.get('attribute-list', []):
                    self._recordings_cache.pop(rec_id, None)
                    continue
                try:
                    fetched_recording = self._get_recording(
                        rec_id,
                        deadline,
                        'related recording {}/{}'.format(index, total),
                    )
                except ScanTimedOut as exc:
                    self._raise_timeout_with_partial(exc, oldest_date)
                except Exception as exc:
                    self._status('Skipping related recording {}/{} after {}: {}.'.format(
                        index, total, type(exc).__name__, exc))
                    continue
                if not _contains_artist(fetched_recording, artist_ids):
                    self._recordings_cache.pop(rec_id, None)
                    continue
            elif 'attribute-list' in rec and (
                    self.config['filter_recordings'] or 'cover' in rec.get('attribute-list', [])):
                self._recordings_cache.pop(rec_id, None)
                continue

            if not fetched_recording:
                try:
                    fetched_recording = self._get_recording(
                        rec_id,
                        deadline,
                        'related recording {}/{}'.format(index, total),
                    )
                except ScanTimedOut as exc:
                    self._raise_timeout_with_partial(exc, oldest_date)
                except Exception as exc:
                    self._status('Skipping related recording {}/{} after {}: {}.'.format(
                        index, total, type(exc).__name__, exc))
                    continue

            for release in _mapping(fetched_recording).get('release-list', []):
                release = _mapping(release)
                if release_types is not None and release.get('status') not in release_types:
                    continue

                release_date = release.get('date')
                if not release_date:
                    continue

                try:
                    date = DateWrapper(iso_string=release_date)
                    if date < oldest_date:
                        oldest_date = date
                except ValueError:
                    self._log.error(
                        'Could not parse date {0} for recording {1}',
                        release_date,
                        rec,
                    )

            self._recordings_cache.pop(rec_id, None)

        return oldest_date

    def _iterate_dates(self, recordings, starting_date, is_cover, artist_ids, deadline):
        approach = self.config['approach'].get()
        oldest_date = starting_date

        if approach not in ('recordings', 'releases', 'hybrid', 'both'):
            raise SkipTrack('invalid oldestdate approach: {}'.format(approach))

        if approach in ('recordings', 'hybrid', 'both'):
            try:
                oldest_date = self._extract_oldest_recording_date(
                    recordings,
                    starting_date,
                    is_cover,
                    approach,
                    deadline,
                )
            except ScanTimedOut as exc:
                self._raise_timeout_with_partial(exc, oldest_date)

        if approach in ('releases', 'both') or (
                approach == 'hybrid' and oldest_date == starting_date):
            try:
                oldest_date = self._extract_oldest_release_date(
                    recordings,
                    oldest_date,
                    is_cover,
                    artist_ids,
                    deadline,
                )
            except ScanTimedOut as exc:
                self._raise_timeout_with_partial(exc, oldest_date)

        return None if oldest_date == DateWrapper.today() else oldest_date

    def _get_oldest_date(self, recording_id, item_date, deadline=None):
        recording = self._get_recording(
            recording_id,
            deadline,
            'selected recording',
        )
        is_cover = _is_cover(recording)
        work_id = _get_work_id_from_recording(recording)
        artist_ids = _get_artist_ids_from_recording(recording)
        today = DateWrapper.today()

        # A file date is used only when explicitly enabled, and only after it
        # survives _item_date_or_none. Blank tags therefore do not become 0001.
        starting_date = item_date if item_date is not None and (
            self.config['use_file_date'] or not work_id
        ) else today

        if not work_id:
            self._status('No linked work ID; checking only the selected recording.')
            return self._iterate_dates(
                [recording],
                starting_date,
                is_cover,
                artist_ids,
                deadline,
            )

        work = self._fetch_work(work_id, deadline)
        recordings = _mapping(work).get('recording-relation-list')
        if not recordings:
            raise SkipTrack(
                'MusicBrainz work {} has no valid associated recordings'.format(work_id)
            )

        max_related = int(self.config['max_related_recordings'].get())
        count = len(recordings)
        self._status('Work {} has {} linked recordings.'.format(work_id, count))

        if max_related > 0 and count > max_related:
            self._status(
                'Linked recordings exceed max_related_recordings {}; scanning the first {} of {}.'.format(
                    max_related,
                    max_related,
                    count,
                )
            )
            recordings = recordings[:max_related]

        return self._iterate_dates(
            recordings,
            starting_date,
            is_cover,
            artist_ids,
            deadline,
        )

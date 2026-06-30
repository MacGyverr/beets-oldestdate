# beets-oldestdate

Beets plugin that fetches oldest recording or release date for each track. This is especially useful when tracks are
from best-of compilations, remasters, or re-releases. Originally based on `beets-recordingdate` by tweitzel, but almost
entirely rewritten to actually work with MusicBrainz's incomplete information. The only thing left intact is
the `recording_` MP3 tags, for compatibility with `beets-recordingdate`.

# Patched behavior
Mostly patched by ChatGPT5.5, this solves some of the issues I had with running this awesome plugin.
I was seeing stalls and lockups, this is what I walked the LLM into fixing.
This fork adds safer, more visible processing for large or incomplete MusicBrainz data:

* Prints per-track progress while fetching works, recordings, and release dates. (as a setting if wanted)
* Treats missing, zero, and implausibly old embedded years as unknown instead of allowing them to become year `0001`.
* Preserves the oldest valid date found so far when a release scan reaches `max_scan_seconds`. (was stalling forever or quiting, this at least tries to pick the oldest)
* Logs tracks skipped due to network, data, or processing failures to `oldestdate-skipped.txt` in Beetsâ€™ configured directory. (so you can go back and play with the settings later on some of the files)
* Uses `max_related_recordings` as a scan limit: large works are processed up to the configured number of related recordings rather than skipped outright.
* Handles partial MusicBrainz dates such as `2015-??-??` and `2015-07-??` without inventing missing month or day values.
* Avoids crashing on mixed MusicBrainz artist-credit entries such as featured-artist separators.


# Installation

The plugin is intended to be used in singleton mode. Undefined behaviour may occur otherwise.
Simply run `pip install beets-oldestdate` then add `oldestdate` to the list of active plugins in beets and configure as
necessary. 
Then download this script and run the following:
PLUGIN="/home/user/.local/pipx/venvs/beets/lib/python3.10/site-packages/beetsplug/oldestdate.py" `or whereever you have the original plugin installed to, this is a linux path`
NEW="/home/user/Downloads/oldestdate.py" `or where you downloaded the .py of this updated script to`

cp "$PLUGIN" "${PLUGIN}.before_patch"
cp "$NEW" "$PLUGIN"

python3 -m py_compile "$PLUGIN"
beet version
If it gives you a version, then it should work fine.

# Configuration

|          Key           | Default Value |                                                                                                                          Description                                                                                                                           |
|:----------------------:|:-------------:|:--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
|          auto          |     True      |                                                                                                             Run oldestdate during the import phase                                                                                                             |
|    ignore_track_id     |     False     |                                                                           During import, ignore existing track_id. Needed if using plugin on a library already tagged by MusicBrainz                                                                           |
|    filter_on_import    |     True      |                                                                       During import, weight down candidates with no work_id so you are more likely to choose a recording with a work_id                                                                        |
| prompt_missing_work_id |     True      |                                                                                             During import, prompt to fix work_id if missing from chosen recording                                                                                              |
|         force          |     False     |                                                                                              Run even if `recording_` tags have already been applied to the track                                                                                              |
|     overwrite_date     |     False     |                                                                                                Overwrite the date MP3 tag field, inluding year, month, and day                                                                                                 |
|    overwrite_month     |     True      |                                                                                             If overwriting date, also overwrite month field, otherwise leave blank                                                                                             |
|     overwrite_day      |     True      |                                                                                              If overwriting date, also overwrite day field, otherwise leave blank                                                                                              |
|   filter_recordings    |     True      |                                                                                   Skip recordings that have attributes before fetching them. This is usually live recordings                                                                                   |
|        approach        |   releases    | What approach to use to find oldest date. Possible values: `recordings, releases, hybrid, both`. `recordings` works like `beets-recordingdate` did, `releases` is a far more accurate method. Hybrid only fetches releases if no date was found in recordings. |
|     release_types      |     None      |                                                                                                Filter releases by type, e.g. `['Official']`. Usually not needed                                                                                                |
|     use_file_date      |     False     |                                                                                               Use the file's embedded date too when looking for the oldest date                                                                                                |
|  max_network_retries   |       3       |                                                                           Maximum amount of times a given network call will be retried, using exponential backoff, before giving up.                                                                           |
|      show_progress     |      no       |                                                                                                                         Displays phase/progress lines
|     progress_every     |       1       |                                                                                                               Prints scan status every ten related recordings
| max_related_recordings |      200      |                                                                                                     Limits a giant MusicBrainz work instead of crawling it indefinitely
|    max_scan_seconds    |      120      |                                                                                                             Gives each track up to two minutes before skipping it
|    request_timeout     |      20       |                                                                                                      	Limits a stalled MusicBrainz socket request to 20 seconds
|    network_retries     |       2       |                                                                                                            	Two visible attempts per MusicBrainz request
|   minimum_file_year    |     1000      |                                                                                                  	Treats 0, blank, and absurdly old embedded years as unknown
## Optimal Configuration

    musicbrainz:
      searchlimit: 20
    plugins: oldestdate

    oldestdate:
      auto: yes
      ignore_track_id: yes
      filter_on_import: yes
      prompt_missing_work_id: yes
      force: yes
      overwrite_date: yes
      overwrite_month: yes
      overwrite_day: yes
      filter_recordings: yes
      approach: 'releases'
	  show_progress: no  #yes will show what is going on in the background.
	  progress_every: 1  #will show how many lines of what is going on, 1 to show it all
	  max_related_recordings: 200 #Skips anything above this for a giant MusicBrainz work instead of crawling it indefinitely
	  max_scan_seconds: 120 #Gives each track up to two minutes before skipping it
	  request_timeout: 20 #Limits a stalled MusicBrainz socket request to 20 seconds
	  network_retries: 2 #Two visible attempts per MusicBrainz request
	  minimum_file_year: 1000 #Treats `0`, blank, and absurdly old embedded years as unknown   

## How it works

The plugin will take the recording that was chosen and get its `work_id`. From this, it gets all recordings associated
with said work. If using the `recordings` approach, it will look through these recordings' dates and find the oldest. If
using the `releases` approach, it will instead go through the dates for all releases for all recordings and find the
oldest (*much* more accurate). The difference between these two approaches is that with `recordings` it only takes one
API call to get the necessary data, while with `releases` it takes *n* calls, where *n* is the number of recordings.
This takes significantly longer due to MusicBrainz's default ratelimit of 1 API call per second. Due to this, the
option `filter_recordings` exists to cut down on the amount of calls needed.

### Missing work_id

If the chosen recording has no Work associated with it, the plugin cannot do its job. This is where `filter_on_import`
comes in: it applies a negative score to tracks that don't have an associated work so they are much less likely to be
chosen. However, this means some of the displayed tracks will be irrelevant. Thus, setting the `searchlimit` to 20 or so
tracks is needed to hit the one recording that *does* have a work. This happens to work quite well with famous songs
because there is usually a single recording with an associated work that is the original recording, and thus the oldest.
If we match with this one, the other recordings that we can't get to because they are not associated with the same work
are irrelevant, because we already have the oldest date.

However, it sometimes happens that there is no available recording that matches our track with an associated work. This
is what `prompt_missing_work_id` is for: it will prompt us to either just use the single matched recording, in which
case only the matched recording's data is used, and checked against the embedded date, or we can try again, or skip the
track. Trying again is so that we may go to the website and amend the data, so that the recordings will have an
associated work. To help with this process, the plugin prints out a URL to a search for that specific track. Your task
is to create a work and associate it with all the relevant recordings, then press try again. This can be quite a
laborious task, so if we see that the date printed by the plugin as being the oldest date found with just the selected
recording seems accurate, choosing `Use this recording` would be the best choice.

### Covers

The plugin is also programmed to deal with covers effectively. Because a `work` actually contains both the recordings of
a song by the original author and any cover artists, when the song we are processing is not a cover, any recordings
tagged as covers are discarded, to save API calls. Conversely, if the processed song *is* a cover, then we only keep
cover recordings, and filter them by author, so only the relevant recordings are kept. This is so the oldest date for a
cover will be the oldest date in which that cover was made, and not the original song. This only works when
in `releases` mode, as we need to fetch the recordings to get the author data. In `recordings` mode, all covers are
treated as the same, even if they may be from different authors.

# Copyright (C) 2011-2015  Edward G. Bruck <ed.bruck1@gmail.com>
#
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

import urllib.request, urllib.parse, urllib.error
import os
import time
import redis
import socket
import json

REDIS_SERVER = "redis"
REDIS_PORT = 6379
REDIS_TIMEOUT = 30
RB_PODCAST_POS_SAVE_INTERVAL = 60
SECONDS_IN_DAY = 86400
PURGE_TIME = 30 * SECONDS_IN_DAY

from gi.repository import GObject, Peas, Notify
from gi.repository import RB


class PodcastPos(GObject.Object, Peas.Activatable):
    __gtype_name__ = 'PodcastPosPlugin'

    object = GObject.property(type=GObject.Object)

    def __init__(self):
        GObject.Object.__init__(self)
        Notify.init("PodcastPosPlugin")

        self.backup_file = os.path.expanduser('~') + '/.rb-podcast-pos.json'
        self.last_save = int(time.time())
        self.hostname = socket.gethostname()

        # override if env variable exists
        self.redis_server = os.getenv('REDIS_SERVER')
        if not self.redis_server:
            self.redis_server = REDIS_SERVER

        try:
            redis_conn = redis.Redis(self.redis_server, socket_timeout=REDIS_TIMEOUT, port=REDIS_PORT)
            self.pos_dict = json.loads(redis_conn.get('rb-podcast-pos:data').decode('utf8'))
            Notify.Notification.new("Loaded " + str(len(self.pos_dict)) + " entries from redis").show()
        except:
            try:
                self.pos_dict = json.load(open(self.backup_file))
            except:
                self.pos_dict = {}

    def purge_missing_and_save(self):
        to_purge = []

        for key in self.pos_dict:
            # todo: remove the time check and only use for orphaned files after a lengthy interval
            # if now - self.pos_dict[key]['timestamp'] >= PURGE_TIME:
            if self.hostname in self.pos_dict[key]['hosts']:
                if not os.path.isfile(urllib.parse.unquote(key[7:])):
                    self.pos_dict[key]['hosts'].remove(self.hostname)
                    # if all hosts are gone, so we purge this entry
                    if len(self.pos_dict[key]['hosts']) == 0:
                        to_purge.append(key)

        if len(to_purge):
            try:
                redis_conn = redis.Redis(self.redis_server, socket_timeout=REDIS_TIMEOUT, port=REDIS_PORT)
            except:
                redis_conn = None

            purge_date_str = time.strftime("%c")
            for key in to_purge:
                del self.pos_dict[key]
                if redis_conn:
                    redis_conn.append('rb-podcast-pos:purged', purge_date_str + ", " + str(key) + '\n')

        self.save_podcast_pos()

    def do_activate(self):
        shell = self.object
        shell_player = shell.props.shell_player

        self.db = shell.props.db
        self.psc_id1 = shell_player.connect('playing-song-changed', self.playing_song_changed)
        self.psc_id2 = shell_player.connect('elapsed-changed', self.elapsed_changed)
        self.pcs_id3 = shell_player.connect('playing-changed', self.playing_changed)

    @staticmethod
    def get_song_info(entry):
        song = {
            'genre': entry.get_string(RB.RhythmDBPropType.GENRE),
            'duration': entry.get_ulong(RB.RhythmDBPropType.DURATION),
            'location': entry.get_playback_uri(),
            'album': entry.get_string(RB.RhythmDBPropType.ALBUM),
            'title': entry.get_string(RB.RhythmDBPropType.TITLE)
        }
        return song

    def do_deactivate(self):
        self.purge_missing_and_save()
        Notify.Notification.new("Saved " + str(len(self.pos_dict)) + " entries to redis").show()

        Notify.uninit()

        shell = self.object
        self.psc_id1 = None
        self.psc_id2 = None
        self.psc_id3 = None
        self.db = None

    def playing_changed(self, player, playing):
        entry = player.get_playing_entry()
        if entry:
            song_info = self.get_song_info(entry)
            if not playing:
                if 'Podcast' == song_info['genre']:
                    self.save_podcast_pos()

    def playing_song_changed(self, player, entry):
        if entry:
            song_info = self.get_song_info(entry)
            if song_info['location'] in self.pos_dict:
                new_pos = self.pos_dict[song_info['location']]['pos']

                # add new host
                if self.hostname not in self.pos_dict[song_info['location']]['hosts']:
                    self.pos_dict[song_info['location']]['hosts'].append(self.hostname)

                if new_pos >= song_info['duration'] - 1:
                    return

                # I'm sure there is a better way...
                n = 0
                while n < 10:
                    try:
                        player.set_playing_time(new_pos)
                        break
                    except:
                        time.sleep(0.1)
                        n += 1

    def elapsed_changed(self, player, pos):
        if pos > 0:
            entry = player.get_playing_entry()

            if entry:
                song_info = self.get_song_info(entry)
                if 'Podcast' == song_info['genre'] and song_info['location'].startswith('file://'):
                    now = int(time.time())

                    if song_info['location'] not in self.pos_dict:
                        self.pos_dict[song_info['location']] = {'timestamp': now, 'pos': pos, 'hosts': [self.hostname]}
                    else:
                        self.pos_dict[song_info['location']]['pos'] = pos

                    if now - self.last_save >= RB_PODCAST_POS_SAVE_INTERVAL:
                        self.save_podcast_pos()

    def save_podcast_pos(self):
        json_data = json.dumps(self.pos_dict, indent=4)
        open(self.backup_file, 'w').write(json_data)

        try:
            redis_conn = redis.Redis(self.redis_server, socket_timeout=REDIS_TIMEOUT, port=REDIS_PORT)
            redis_conn.set('rb-podcast-pos:data', json_data)
            redis_conn.set('rb-podcast-pos:log',
                           self.hostname + '@' + time.strftime('%c') + ', count=' + str(len(self.pos_dict)))
        except:
            pass

        self.last_save = int(time.time())

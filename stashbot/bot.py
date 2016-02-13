# -*- coding: utf-8 -*-
#
# This file is part of bd808's stashbot application
# Copyright (C) 2015 Bryan Davis and contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
"""IRC bot"""

import elasticsearch
import irc.bot
import irc.buffer
import irc.client
import irc.strings
import re
import time

from . import phab

RE_STYLE = re.compile(r'[\x02\x0F\x16\x1D\x1F]|\x03(\d{,2}(,\d{,2})?)?')
RE_PHAB = re.compile(r'\b(T\d+)\b')
RE_PHAB_NOURL = re.compile(r'(?:[^/])\b([DMPT]\d+)\b')

class Stashbot(irc.bot.SingleServerIRCBot):
    def __init__(self, config, logger):
        """Create bot.

        :param config: Dict of configuration values
        :param logger: Logger
        """
        self.config = config
        self.logger = logger

        self.es = elasticsearch.Elasticsearch(
            self.config['elasticsearch']['servers'],
            **self.config['elasticsearch']['options']
        )

        self.phab = phab.Client(
            self.config['phab']['url'],
            self.config['phab']['user'],
            self.config['phab']['cert']
        )

        # Ugh. A UTF-8 only world is a nice dream but the real world is all
        # yucky and full of legacy encoding issues that should not crash my
        # bot.
        irc.client.ServerConnection.buffer_class = \
            irc.buffer.LenientDecodingLineBuffer

        super(Stashbot, self).__init__(
            [(self.config['irc']['server'], self.config['irc']['port'])],
            self.config['irc']['nick'],
            self.config['irc']['realname']
        )

    def get_version(self):
        return 'Stashbot'

    def on_nicknameinuse(self, conn, event):
        nick = conn.get_nickname()
        self.logger.warning('Requested nick "%s" in use', nick)
        conn.nick(nick + '_')

    def on_welcome(self, conn, event):
        self.logger.info('Connected to server %s', conn.get_server_name())
        if 'password' in self.config['irc']:
            self.logger.debug('Authenticating with Nickserv')
            conn.privmsg('NickServ', 'identify %s %s' % (
                self.config['irc']['nick'], self.config['irc']['password']))

        for c in self.config['irc']['channels']:
            self.logger.debug('Joining %s', c)
            conn.join(c)

    def on_join(self, conn, event):
        self.logger.warning('Joined channel %s', event.target)

    def on_pubmsg(self, conn, event):
        # Log all public channel messages we receive
        doc = self._event_to_doc(conn, event)
        self.do_logmsg(conn, event, doc)

        # Look for special messages
        msg = event.arguments[0]
        if msg.startswith('!log '):
            self.do_banglog(conn, event, doc)

        elif msg.startswith('!bash '):
            self.do_bash(conn, event, doc)

        elif self.config['phab']['echo'] and RE_PHAB_NOURL.match(msg):
            self.do_phabecho(conn, event, doc)

    def on_privmsg(self, conn, event):
        msg = event.arguments[0]
        if msg.startswith('!bash '):
            doc = self._event_to_doc(conn, event)
            self.do_bash(conn, event, doc)
        else:
            self._respond(conn, event, event.arguments[0][::-1])

    def do_logmsg(self, conn, event, doc):
        """Log an IRC channel message to Elasticsearch."""
        fmt = self.config['elasticsearch']['index']
        self.es.index(
            index=time.strftime(fmt, time.gmtime()),
            doc_type='irc',
            body=doc,
            consistency='one'
        )

    def do_banglog(self, conn, event, doc):
        """Process a !log message"""
        bang = dict(doc)
        # Trim '!log ' from the front of the message
        msg = bang['message'][5:]
        bang['type'] = 'sal'

        if bang['channel'] == '#wikimedia-labs':
            project, msg = msg.split(None, 1)
            bang['project'] = project
            bang['message'] = msg
        elif bang['channel'] == '#wikimedia-releng':
            bang['project'] = 'releng'
            bang['message'] = msg
        elif bang['channel'] == '#wikimedia-analytics':
            bang['project'] = 'analytics'
            bang['message'] = msg
        elif bang['channel'] == '#wikimedia-operations':
            bang['project'] = 'production'
            bang['message'] = msg
            if bang['nick'] == 'logmsgbot':
                nick, msg = msg.split(None, 1)
                bang['nick'] = nick
                bang['message'] = msg
        else:
            self.logger.warning(
                '!log message on unexpected channel %s', bang['channel'])
            self._respond(conn, event, 'Not expecting to hear !log here')
            return

        ret = self.es.index(
            index='sal', doc_type='sal', body=bang, consistency='one')

        if ('phab' in self.config['sal'] and
            'created' in ret and ret['created'] == True
        ):
            m = RE_PHAB.findall(bang['message'])
            msg = self.config['sal']['phab'] % dict(
                {'href': self.config['sal']['view_url'] % ret['_id']},
                **bang
            )
            for task in m:
                try:
                    self.phab.comment(task, msg)
                except:
                    self.logger.exception('Failed to add note to phab task')

    def do_bash(self, conn, event, doc):
        """Process a !bash message"""
        bash = dict(doc)
        # Trim '!bash ' from the front of the message
        msg = bash['message'][6:]
        # Expand tabs to line breaks
        bash['message'] = msg.replace("\t", "\n").strip()
        bash['type'] = 'bash'
        bash['up_votes'] = 0
        bash['down_votes'] = 0
        bash['score'] = 0
        # Remove unneeded irc fields
        del bash['user']
        del bash['channel']
        del bash['server']
        del bash['host']

        ret = self.es.index(
            index='bash', doc_type='bash', body=bash, consistency='one')

        if 'created' in ret and ret['created'] == True:
            self._respond(conn, event,
                '%s: Stored quip at %s' % (
                    event.source.nick,
                    self.config['bash']['view_url'] % ret['_id']
                )
            )
        else:
            self.logger.error('Failed to save document: %s', ret)
            self._respond(conn, event,
                '%s: Yuck. Something blew up when I tried to save that.' % (
                    event.source.nick,
                )
            )

    def do_phabecho(self, conn, event, doc):
        """Give links to Phabricator tasks"""
        for task in RE_PHAB_NOURL.findall(doc['message']):
            try:
                info = self.taskInfo(task)
            except:
                self.logger.exception('Failed to lookup info for %s', task)
            else:
                self._respond(conn, event, self.config['phab']['echo'] % info)

    def _respond(self, conn, event, msg):
        to = event.target
        if to == self.connection.get_nickname():
            to = event.source.nick
        conn.privmsg(to, msg)

    def _event_to_doc(self, conn, event):
        """Make an Elasticsearch document from an IRC event."""
        return {
            'message': RE_STYLE.sub('', event.arguments[0]),
            '@timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'type': 'irc',
            'user': event.source,
            'channel': event.target,
            'nick': event.source.nick,
            'server': conn.get_server_name(),
            'host': event.source.host,
        }

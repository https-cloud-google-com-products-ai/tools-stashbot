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

import datetime
import ldap3
import re
import time
import twitter

from . import acls
from . import mediawiki

RE_PHAB = re.compile(r'\b(T\d+)\b')


class Logger(object):
    def __init__(self, irc, phab, es, config, logger):
        self.irc = irc
        self.phab = phab
        self.es = es
        self.config = config
        self.logger = logger

        self.ldap = ldap3.Connection(
            self.config['ldap']['uri'],
            auto_bind=True
        )
        self.wikis = {}
        self.twitter_clients = {}
        self.projects = None

    def log(self, conn, event, doc):
        """Process a !log message"""
        bang = dict(doc)
        channel = bang['channel']

        channel_conf = self._get_sal_config(channel)

        if 'project' not in channel_conf:
            self.logger.warning(
                '!log message on unexpected channel %s', channel)
            self.irc.respond(conn, event, 'Not expecting to hear !log here')
            return

        if not self._check_sal_acl(channel, event.source):
            self.logger.warning(
                'Ignoring !log from %s in %s', event.source, channel)
            self.irc.respond(
                conn,
                event,
                '%s: The !log ACLs in this channel blocked your message' % (
                    bang['nick'])
            )
            return

        # Trim '!log ' from the front of the message
        bang['message'] = bang['message'][5:].strip()
        bang['type'] = 'sal'
        bang['project'] = channel_conf['project']

        if bang['message'] == '':
            self.irc.respond(
                conn, event, 'Message missing. Nothing logged.')
            return

        if bang['nick'] == 'logmsgbot':
            # logmsgbot is expected to tell us who is running the command
            bang['nick'], bang['message'] = bang['message'].split(None, 1)

        if channel == '#wikimedia-labs':
            bang['project'], bang['message'] = bang['message'].split(None, 1)
            if bang['project'] not in self._get_projects():
                self.logger.warning('Invalid project %s', bang['project'])
                tool = 'tools.%s' % bang['project']
                if tool in self._get_projects():
                    self.irc.respond(
                        conn,
                        event,
                        'Did you mean %s instead of %s?' % (
                            tool, bang['project'])
                    )
                return

            if bang['project'] == 'deployment-prep':
                bang['project'] = 'releng'

        self._store_in_es(bang)

        if 'wiki' in channel_conf:
            try:
                self._write_to_wiki(conn, event, bang, channel_conf)
            except Exception:
                self.logger.exception('Error writing to wiki')
                self.irc.respond(
                    conn, event,
                    'Failed to log message to wiki. '
                    'Somebody should check the error logs.')

        if 'twitter' in channel_conf:
            try:
                self._tweet(bang, channel_conf)
            except Exception:
                self.logger.exception('Error writing to twitter')

    def _get_sal_config(self, channel):
        """Get SAL configuration for given channel."""
        if 'channels' not in self.config['sal']:
            return {}
        if channel not in self.config['sal']['channels']:
            return {}
        return self.config['sal']['channels'][channel]

    def _check_sal_acl(self, channel, source):
        """Check a message source against a channel's acl list"""
        conf = self._get_sal_config(channel)
        if 'acl' not in conf:
            return True
        if channel not in conf['acl']:
            return True
        return acls.check(conf['acl'], source)

    def _get_projects(self):
        """Get a list of valid Labs projects"""
        if self.projects and self.projects[0] + 300 > time.time():
            # Expire cache
            self.projects = None

        if self.projects is None:
            projects = self._get_ldap_names('projects')
            servicegroups = self._get_ldap_names('servicegroups')
            self.projects = (time.time(), projects + servicegroups)

        return self.projects[1]

    def _get_ldap_names(self, ou):
        """Get a list of cn values from LDAP for a given ou."""
        dn = 'ou=%s,%s' % (ou, self.config['ldap']['base'])
        success = self.ldap.search(
            dn,
            '(objectclass=groupofnames)',
            attributes=['cn']
        )
        if success:
            return [g.cn for g in self.ldap.entries]
        else:
            self.logger.error('Failed to get LDAP data for %s', dn)
            return []

    def _store_in_es(self, bang):
        """Save a !log message to elasticsearch."""
        ret = self.es.index(index='sal', doc_type='sal', body=bang)
        if ('phab' in self.config['sal'] and
            'created' in ret and ret['created'] is True
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

    def _write_to_wiki(self, conn, event, bang, channel_conf):
        """Write a !log message to a wiki page."""
        now = datetime.datetime.utcnow()
        section = now.strftime('== %Y-%m-%d ==')
        logline = '* %02d:%02d %s: %s' % (
            now.hour, now.minute, bang['nick'], bang['message'])
        summary = '%(nick)s: %(message)s' % bang

        site = self._get_mediawiki_client(channel_conf['wiki'])
        page = site.get_page(channel_conf['page'] % bang)

        text = page.text()
        lines = text.split('\n')
        first_header = 0

        for pos, line in enumerate(lines):
            if line.startswith('== '):
                first_header = pos

        if lines[first_header] == section:
            lines.insert(first_header + 1, logline)
        else:
            lines.insert(first_header, '')
            lines.insert(first_header, logline)
            lines.insert(first_header, section)

        if 'category' in channel_conf:
            cat = channel_conf['category']
            if not re.search(r'\[\[Category:%s\]\]' % cat, text):
                lines.append(
                    '<noinclude>[[Category:%s]]</noinclude>' % cat)

        resp = page.save('\n'.join(lines), summary=summary, bot=True)
        if resp['result'] == 'Success':
            self.irc.respond(
                conn, event, 'Logged the message at %(title)s' % resp)
        else:
            self.logger.error('Failed to edit %s: %s', page.name, resp)
            self.irc.respond(
                conn, event, 'Error updating %s' % page.name)

    def _tweet(self, bang, channel_conf):
        """Post a tweet."""
        update = ('%(nick)s: %(message)s' % bang)[:140]
        client = self._get_twitter_client(channel_conf['twitter'])
        client.PostUpdate(update)

    def _get_mediawiki_client(self, domain):
        """Get a mediawiki client for the given domain."""
        if domain not in self.wikis:
            conf = self.config['mediawiki'][domain]
            self.wikis[domain] = mediawiki.Client(
                conf['url'],
                consumer_token=conf['consumer_token'],
                consumer_secret=conf['consumer_secret'],
                access_token=conf['access_token'],
                access_secret=conf['access_secret']
            )
        return self.wikis[domain]

    def _get_twitter_client(self, name):
        """Get a twitter client."""
        if name not in self.twitter_clients:
            conf = self.config['twitter'][name]
            self.twitter_clients[name] = twitter.Api(
                consumer_key=conf['consumer_key'],
                consumer_secret=conf['consumer_secret'],
                access_token_key=conf['access_token_key'],
                access_token_secret=conf['access_token_secret']
            )
        return self.twitter_clients[name]

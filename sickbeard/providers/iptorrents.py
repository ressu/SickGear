# Author: seedboy
# URL: https://github.com/seedboy
#
# This file is part of SickGear.
#
# SickGear is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickGear is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickGear.  If not, see <http://www.gnu.org/licenses/>.

import re
import traceback
import datetime

import sickbeard
import generic
from sickbeard.common import Quality
from sickbeard import logger, tvcache, db, classes, helpers, show_name_helpers
from sickbeard.exceptions import ex, AuthException
from lib import requests
from lib.requests import exceptions
from sickbeard.bs4_parser import BS4Parser
from lib.unidecode import unidecode
from sickbeard.helpers import sanitizeSceneName
from sickbeard.show_name_helpers import allPossibleShowNames


class IPTorrentsProvider(generic.TorrentProvider):
    urls = {'base_url': 'https://iptorrents.eu',
            'login': 'https://iptorrents.eu/torrents/',
            'search': 'https://iptorrents.eu/t?%s%s&q=%s&qf=ti#torrents'}

    def __init__(self):
        generic.TorrentProvider.__init__(self, 'IPTorrents', True, False)
        self.username = None
        self.password = None
        self.ratio = None
        self.freeleech = False
        self.cache = IPTorrentsCache(self)
        self.url = self.urls['base_url']
        self.categorie = 'l73=1&l78=1&l66=1&l65=1&l79=1&l5=1&l4=1'

    def getQuality(self, item, anime=False):

        quality = Quality.sceneQuality(item[0], anime)
        return quality

    def _checkAuth(self):

        if not (self.username and self.password):
            raise AuthException('Your authentication credentials for ' + self.name + ' are missing, check your config.')

        return True

    def _doLogin(self):

        if any(self.session.cookies.values()):
            return True

        login_params = {'username': self.username, 'password': self.password, 'login': 'submit'}

        response = helpers.getURL(self.urls['login'], post_data=login_params, session=self.session)

        if not response or re.search('tries left', response) or re.search('<title>IPT</title>', response):
            logger.log(u'Could not authenticate %s, abort provider.' % self.name, logger.ERROR)
            return False

        return True

    def _get_season_search_strings(self, ep_obj):

        search_string = {'Season': []}
        for show_name in set(show_name_helpers.allPossibleShowNames(self.show)):
            if ep_obj.show.air_by_date or ep_obj.show.sports:
                ep_string = show_name + ' ' + str(ep_obj.airdate).split('-')[0]
            elif ep_obj.show.anime:
                ep_string = show_name + ' ' + "%d" % ep_obj.scene_absolute_number
            else:
                ep_string = show_name + ' S%02d' % int(ep_obj.scene_season)  #1) showName SXX

            search_string['Season'].append(ep_string)

        return [search_string]

    def _get_episode_search_strings(self, ep_obj, add_string=''):

        search_string = {'Episode': []}

        if not ep_obj:
            return []

        if self.show.air_by_date:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            str(ep_obj.airdate).replace('-', '|')
                search_string['Episode'].append(ep_string)
        elif self.show.sports:
            for show_name in set(allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            str(ep_obj.airdate).replace('-', '|') + '|' + \
                            ep_obj.airdate.strftime('%b')
                search_string['Episode'].append(ep_string)
        elif self.show.anime:
            for show_name in set(show_name_helpers.allPossibleShowNames(self.show)):
                ep_string = sanitizeSceneName(show_name) + ' ' + \
                            "%i" % int(ep_obj.scene_absolute_number)
                search_string['Episode'].append(ep_string)
        else:
            for show_name in set(show_name_helpers.allPossibleShowNames(self.show)):
                ep_string = show_name_helpers.sanitizeSceneName(show_name) + ' ' + \
                            sickbeard.config.naming_ep_type[2] % {'seasonnumber': ep_obj.scene_season,
                                                                  'episodenumber': ep_obj.scene_episode} + ' %s' % add_string

                search_string['Episode'].append(re.sub('\s+', ' ', ep_string))

        return [search_string]

    def _doSearch(self, search_params, search_mode='eponly', epcount=0, age=0):

        results = []
        items = {'Season': [], 'Episode': [], 'RSS': []}

        freeleech = '&free=on' if self.freeleech else ''

        if not self._doLogin():
            return []

        for mode in search_params.keys():
            for search_string in search_params[mode]:

                # URL with 50 tv-show results, or max 150 if adjusted in IPTorrents profile
                if isinstance(search_string, unicode):
                    search_string = unidecode(search_string)
                searchURL = '%s%s' % (self.urls['search'] % (self.categorie, freeleech, search_string),
                                      (';o=seeders', '')['RSS' == mode])

                logger.log(u"" + self.name + " search page URL: " + searchURL, logger.DEBUG)

                data = self.getURL(searchURL)
                if not data:
                    continue

                try:
                    data = re.sub(r'<button.+?<[\/]button>', '', data, 0, re.IGNORECASE | re.MULTILINE)
                    with BS4Parser(data, features=["html5lib", "permissive"]) as html:
                        if not html:
                            logger.log(u"Invalid HTML data: " + str(data), logger.DEBUG)
                            continue

                        if html.find(text='No Torrents Found!'):
                            logger.log(u"No results found for: " + search_string + " (" + searchURL + ")", logger.DEBUG)
                            continue

                        torrent_table = html.find('table', attrs={'class': 'torrents'})
                        torrents = torrent_table.find_all('tr') if torrent_table else []

                        #Continue only if one Release is found
                        if len(torrents) < 2:
                            logger.log(u"The data returned from " + self.name + " does not contain any torrents",
                                       logger.WARNING)
                            continue

                        for result in torrents[1:]:

                            try:
                                torrent = result.find_all('td')[1].find('a')
                                torrent_name = torrent.string
                                torrent_download_url = self.urls['base_url'] + (result.find_all('td')[3].find('a'))['href']
                                torrent_details_url = self.urls['base_url'] + torrent['href']
                                torrent_seeders = int(result.find('td', attrs={'class': 'ac t_seeders'}).string)
                                ## Not used, perhaps in the future ##
                                #torrent_id = int(torrent['href'].replace('/details.php?id=', ''))
                                #torrent_leechers = int(result.find('td', attrs = {'class' : 'ac t_leechers'}).string)
                            except (AttributeError, TypeError):
                                continue

                            # Filter unseeded torrent and torrents with no name/url
                            if mode != 'RSS' and torrent_seeders == 0:
                                continue

                            if not torrent_name or not torrent_download_url:
                                continue

                            item = torrent_name, torrent_download_url
                            logger.log(u"Found result: " + torrent_name + " (" + torrent_details_url + ")", logger.DEBUG)
                            items[mode].append(item)

                except Exception as e:
                    logger.log(u"Failed parsing " + self.name + " Traceback: " + traceback.format_exc(), logger.ERROR)

            results += items[mode]

        return results

    def _get_title_and_url(self, item):

        title, url = item

        if title:
            title = u'' + title
            title = title.replace(' ', '.')

        if url:
            url = str(url).replace('&amp;', '&')

        return (title, url)

    def findPropers(self, search_date=datetime.datetime.today()):

        results = []

        myDB = db.DBConnection()
        sqlResults = myDB.select(
            'SELECT s.show_name, e.showid, e.season, e.episode, e.status, e.airdate FROM tv_episodes AS e' +
            ' INNER JOIN tv_shows AS s ON (e.showid = s.indexer_id)' +
            ' WHERE e.airdate >= ' + str(search_date.toordinal()) +
            ' AND (e.status IN (' + ','.join([str(x) for x in Quality.DOWNLOADED]) + ')' +
            ' OR (e.status IN (' + ','.join([str(x) for x in Quality.SNATCHED]) + ')))'
        )

        if not sqlResults:
            return []

        for sqlshow in sqlResults:
            self.show = helpers.findCertainShow(sickbeard.showList, int(sqlshow["showid"]))
            if self.show:
                curEp = self.show.getEpisode(int(sqlshow["season"]), int(sqlshow["episode"]))
                searchString = self._get_episode_search_strings(curEp, add_string='PROPER|REPACK')

                for item in self._doSearch(searchString[0]):
                    title, url = self._get_title_and_url(item)
                    results.append(classes.Proper(title, url, datetime.datetime.today(), self.show))

        return results

    def seedRatio(self):
        return self.ratio

class IPTorrentsCache(tvcache.TVCache):
    def __init__(self, provider):

        tvcache.TVCache.__init__(self, provider)

        # Only poll IPTorrents every 10 minutes max
        self.minTime = 10

    def _getRSSData(self):
        search_params = {'RSS': ['']}
        return self.provider._doSearch(search_params)


provider = IPTorrentsProvider()

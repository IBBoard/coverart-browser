# -*- Mode: python; coding: utf-8; tab-width: 4; indent-tabs-mode: nil; -*-
#
# Copyright (C) 2014 fossfreedom
# this module has been heavily modifed from rhythmbox context plugin
# Copyright (C) 2009 John Iacona
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.

import os
import urllib.request
import urllib.parse
import json
import gettext

from mako.template import Template
from gi.repository import WebKit
from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import RB
from gi.repository import Gio

import rb
import rb_lastfm as LastFM  # from coverart-search-providers
from coverart_utils import get_stock_size
from coverart_browser_prefs import GSetting
from coverart_browser_prefs import CoverLocale
from coverart_utils import create_button_image


gettext.install('rhythmbox', RB.locale_dir())


def artist_exceptions(artist):
    exceptions = ['various']

    for exception in exceptions:
        if exception in artist.lower():
            return True

    return False


def lastfm_datasource_link(path):
    return "<a href='http://last.fm/'><img src='%s/img/lastfm.png'></a>" % path


LASTFM_NO_ACCOUNT_ERROR = _(
    "Enable LastFM plugin and log in first")


class ArtistInfoWebView(WebKit.WebView):
    def __init(self, *args, **kwargs):
        super(ArtistInfoWebView, self).__init__(*args, **kwargs)

    def initialise(self, source, shell):
        self.source = source
        self.shell = shell

        self.connect("navigation-requested", self.navigation_request_cb)
        self.connect("notify::title", self.view_title_change)

    def view_title_change(self, webview, param):
        print("view_title_change")
        title = webview.get_title()

        if title:
            print("title %s" % title)
            args = json.loads(title)
            artist = args['artist']

            if args['toggle']:
                self.source.album_manager.model.replace_filter('similar_artist', artist)
            else:
                self.source.album_manager.model.remove_filter('similar_artist')
        else:
            print("removing filter")
            self.source.album_manager.model.remove_filter('similar_artist')
        print("end view_title_change")

    def navigation_request_cb(self, view, frame, request):
        # open HTTP URIs externally.  this isn't a web browser.
        print("navigation_request_cb")
        if request.get_uri().startswith('http'):
            print("opening uri %s" % request.get_uri())
            Gtk.show_uri(self.shell.props.window.get_screen(), request.get_uri(), Gdk.CURRENT_TIME)

            return 1  # WEBKIT_NAVIGATION_RESPONSE_IGNORE
        else:
            return 0  # WEBKIT_NAVIGATION_RESPONSE_ACCEPT

    def do_button_release_event(self, *args):
        print("do_release_button")
        WebKit.WebView.do_button_release_event(self, *args)

        return True


class ArtistInfoPane(GObject.GObject):
    __gsignals__ = {
        'selected': (GObject.SIGNAL_RUN_LAST, None,
                     (GObject.TYPE_STRING, GObject.TYPE_STRING))
    }

    paned_pos = GObject.property(type=str)

    min_paned_pos = 100

    def __init__(self, button_box, stack, info_paned, source):
        GObject.GObject.__init__(self)

        self.ds = {}
        self.view = {}

        # self.buttons = button_box
        self.source = source
        self.plugin = source.plugin
        self.shell = source.shell
        self.info_paned = info_paned
        self.current_artist = None
        self.current_album_title = None
        self.current = 'artist'
        self._from_paned_handle = 0

        self.stack = stack
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack_switcher = Gtk.StackSwitcher()
        stack_switcher.set_stack(self.stack)
        self.stack.connect('notify::visible-child-name', self.change_stack)
        button_box.pack_start(stack_switcher, False, False, 0)
        button_box.show_all()

        # cache for artist/album information: valid for a month, can be used indefinitely
        # if offline, discarded if unused for six months
        self.info_cache = rb.URLCache(name='info',
                                      path=os.path.join('coverart_browser', 'info'),
                                      refresh=30,
                                      discard=180)
        # cache for rankings (artist top tracks and top albums): valid for a week,
        # can be used for a month if offline
        self.ranking_cache = rb.URLCache(name='ranking',
                                         path=os.path.join('coverart_browser', 'ranking'),
                                         refresh=7,
                                         lifetime=30)

        self.info_cache.clean()
        self.ranking_cache.clean()

        self.ds['link'] = LinksDataSource()
        self.ds['artist'] = ArtistDataSource(self.info_cache,
                                             self.ranking_cache)

        self.view['artist'] = ArtistInfoView()
        self.view['artist'].initialise(self.source,
                                       self.shell,
                                       self.plugin,
                                       self.stack,
                                       self.ds['artist'],
                                       self.ds['link'])

        self.ds['album'] = AlbumDataSource(self.info_cache,
                                           self.ranking_cache)
        self.view['album'] = AlbumInfoView()
        self.view['album'].initialise(self.source,
                                      self.shell,
                                      self.plugin,
                                      self.stack,
                                      self.ds['album'])

        self.ds['echoartist'] = EchoArtistDataSource(
            self.info_cache,
            self.ranking_cache)
        self.view['echoartist'] = EchoArtistInfoView()
        self.view['echoartist'].initialise(self.source,
                                           self.shell,
                                           self.plugin,
                                           self.stack,
                                           self.ds['echoartist'],
                                           self.ds['link'])

        self.gs = GSetting()
        self.connect_properties()
        self.connect_signals()
        Gdk.threads_add_timeout(GLib.PRIORITY_DEFAULT_IDLE,
                                50,
                                self._change_paned_pos,
                                self.source.viewmgr.view_name)
        self.view[self.current].activate()

    def connect_properties(self):
        '''
        Connects the source properties to the saved preferences.
        '''
        setting = self.gs.get_setting(self.gs.Path.PLUGIN)

        setting.bind(
            self.gs.PluginKey.ARTIST_INFO_PANED_POSITION,
            self,
            'paned-pos',
            Gio.SettingsBindFlags.DEFAULT)

    def connect_signals(self):
        self.tab_cb_ids = []

        # Listen for switch-tab signal from each tab
        '''
        for key, value in self.tab.items():
            self.tab_cb_ids.append(( key, 
                                    self.tab[key].connect ('switch-tab', 
                                                            self.change_tab)
                                    ))
        '''

        # Listen for selected signal from the views
        self.connect('selected', self.select_artist)

        # lets remember info paned click
        self.info_paned.connect('button_press_event',
                                self.paned_button_press_callback)
        self.info_paned.connect('button-release-event',
                                self.paned_button_release_callback)

        # lets also listen for changes to the view to set the paned position
        self.source.viewmgr.connect('new-view', self.on_view_changed)

    def on_view_changed(self, widget, view_name):
        self._change_paned_pos(view_name)

    def _change_paned_pos(self, view_name):
        print(self.paned_pos)
        paned_positions = eval(self.paned_pos)

        found = None
        for viewpos in paned_positions:
            if view_name in viewpos:
                found = viewpos
                break

        if not found:
            return

        values = found.split(":")
        child_width = int(values[1])

        open_type = "closed"
        if len(values) > 2:
            open_type = values[2]
        elif child_width > 0:
            open_type = "opened"

        if open_type == "closed":
            child_width = 0

        calc_pos = self.source.page.get_allocated_width() - child_width
        self.info_paned.set_position(calc_pos)
        self.info_paned.set_visible(True)

    def _get_child_width(self):
        child = self.info_paned.get_child2()
        return child.get_allocated_width()

    def paned_button_press_callback(self, widget, event):
        print('paned_button_press_callback')
        self._from_paned_handle = 1

        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._from_paned_handle = 2

    def paned_button_release_callback(self, *args):
        '''
        Callback when the artist paned handle is released from its mouse click.
        '''
        if self._from_paned_handle == 0:
            return False

        print("paned_button_release_callback")

        paned_positions = eval(self.paned_pos)

        found = None
        for viewpos in paned_positions:
            if self.source.viewmgr.view_name in viewpos:
                found = viewpos
                break

        if not found:
            print("cannot find")
            return True

        values = found.split(':')

        child_width = self.source.page.get_allocated_width() - self.info_paned.get_position()
        print(child_width)
        open_type = "closed"
        print(values)
        if len(values) > 2:
            open_type = values[2]

        if child_width <= self.min_paned_pos and \
                        self._from_paned_handle == 1 and \
                        open_type == "closed":
            # we are dealing with a situation where the pane is already closed
            # or almost closed - just shut the door
            print("we are closed")
            calc_pos = self.source.page.get_allocated_width()
            self.info_paned.set_position(calc_pos)
            return False

        open_type = "closed"
        paned_positions.remove(found)

        if self._from_paned_handle == 2:
            # we are dealing with a double click situation
            new_width = child_width
            if new_width <= self.min_paned_pos:
                if int(values[1]) == 0:
                    new_width = self.min_paned_pos + 1
                else:
                    new_width = int(values[1])

                open_type = "opened"
                child_width = new_width
            else:
                new_width = 0

            calc_pos = self.source.page.get_allocated_width() - new_width

            self.info_paned.set_position(calc_pos)

        if child_width <= self.min_paned_pos and self._from_paned_handle == 1:
            if int(values[1]) == 0:
                child_width = self.min_paned_pos + 1
                open_type = "opened"
            else:
                child_width = 0

            calc_pos = self.source.page.get_allocated_width() - child_width
            self.info_paned.set_position(calc_pos)

        if self._from_paned_handle == 1 and child_width != 0:
            open_type = "opened"

        paned_positions.append(self.source.viewmgr.view_name + \
                               ":" + \
                               str(child_width) + \
                               ":" + \
                               open_type)

        self.paned_pos = repr(paned_positions)

        self._from_paned_handle = 0
        print("End artist_info_paned_button_release_callback")

    def select_artist(self, widget, artist, album_title):
        print("artist %s title %s" % (artist, album_title))
        if self._get_child_width() > self.min_paned_pos:
            self.view[self.current].reload(artist, album_title)
        else:
            self.view[self.current].blank_view()

        self.current_album_title = album_title
        self.current_artist = artist

    def change_stack(self, widget, value):
        child_name = self.stack.get_visible_child_name()
        if child_name and self.current != child_name:
            self.view[self.current].deactivate()
            if self._get_child_width() > self.min_paned_pos:
                self.view[child_name].activate(self.current_artist, self.current_album_title)
            else:
                self.view[child_name].blank_view()

            self.current = child_name


class BaseInfoView(GObject.Object):
    def __init__(self, *args, **kwargs):
        super(BaseInfoView, self).__init__()

    def initialise(self, source, shell, plugin, stack, ds, view_name, view_image):
        self.stack = stack

        self.webview = ArtistInfoWebView()
        self.webview.initialise(source, shell)

        self.info_scrolled_window = Gtk.ScrolledWindow()
        self.info_scrolled_window.props.hexpand = True
        self.info_scrolled_window.props.vexpand = True
        self.info_scrolled_window.set_shadow_type(Gtk.ShadowType.IN)
        self.info_scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.info_scrolled_window.add(self.webview)
        self.info_scrolled_window.show_all()
        self.stack.add_named(self.info_scrolled_window, view_name)

        theme = Gtk.IconTheme()
        default = theme.get_default()
        image_name = 'coverart_browser_' + view_name
        width, height = get_stock_size()
        pixbuf = create_button_image(plugin, view_image)
        default.add_builtin_icon(image_name, width, pixbuf)

        self.stack.child_set_property(self.info_scrolled_window, "icon-name", image_name)

        self.ds = ds
        self.shell = shell
        self.plugin = plugin
        self.file = ""
        self.album_title = None
        self.artist = None
        self.active = False

        plugindir = plugin.plugin_info.get_data_dir()
        self.basepath = "file://" + urllib.request.pathname2url(plugindir)
        self.link_images = self.basepath + '/img/links/'

        self.load_tmpl()
        self.connect_signals()

    def load_tmpl(self):
        pass

    def connect_signals(self):
        pass

    def load_view(self):
        print("load_view")
        print(self.file)
        self.webview.load_string(self.file, 'text/html', 'utf-8', self.basepath)
        print("end load_view")

    def blank_view(self):
        render_file = self.empty_template.render(stylesheet=self.styles)
        self.webview.load_string(render_file, 'text/html', 'utf-8', self.basepath)

    def loading(self, current_artist, current_album_title):
        pass

    def activate(self, artist=None, album_title=None):
        print("activating Artist Tab")
        self.active = True
        self.reload(artist, album_title)

    def deactivate(self):
        print("deactivating Artist Tab")
        self.active = False


class ArtistInfoView(BaseInfoView):
    def __init__(self, *args, **kwargs):
        super(ArtistInfoView, self).__init__(self, *args, **kwargs)

    def initialise(self, source, shell, plugin, stack, ds, link_ds):
        super(ArtistInfoView, self).initialise(source, shell, plugin, stack, ds, "artist", "microphone.png")

        self.link_ds = link_ds

    def loading(self, current_artist, current_album_title):
        cl = CoverLocale()
        # cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        self.link_ds.set_artist(current_artist)
        self.link_ds.set_album(current_album_title)
        self.loading_file = self.loading_template.render(
            artist=current_artist,
            info=_("Loading biography for %s") % current_artist,
            song="",
            basepath=self.basepath)
        self.webview.load_string(self.loading_file, 'text/html', 'utf-8', self.basepath)

    def load_tmpl(self):
        cl = CoverLocale()
        cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        path = rb.find_plugin_file(self.plugin, 'tmpl/artist-tmpl.html')
        empty_path = rb.find_plugin_file(self.plugin, 'tmpl/artist_empty-tmpl.html')
        loading_path = rb.find_plugin_file(self.plugin, 'tmpl/loading.html')
        self.template = Template(filename=path)
        self.loading_template = Template(filename=loading_path)
        self.empty_template = Template(filename=empty_path)
        self.styles = self.basepath + '/tmpl/artistmain.css'

    def connect_signals(self):
        self.air_id = self.ds.connect('artist-info-ready', self.artist_info_ready)

    def artist_info_ready(self, ds):
        # Can only be called after the artist-info-ready signal has fired.
        # If called any other time, the behavior is undefined
        # try:
        info = ds.get_artist_info()

        small, med, big = info['images'] or (None, None, None)
        summary, full_bio = info['bio'] or (None, None)

        link_album = self.link_ds.get_album()
        if not link_album:
            link_album = ""

        links = self.link_ds.get_album_links()
        if not links:
            links = {}

        cl = CoverLocale()
        cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        self.file = self.template.render(artist=ds.get_current_artist(),
                                         error=ds.get_error(),
                                         image=med,
                                         fullbio=full_bio,
                                         shortbio=summary,
                                         datasource=lastfm_datasource_link(self.basepath),
                                         stylesheet=self.styles,
                                         album=link_album,
                                         art_links=self.link_ds.get_artist_links(),
                                         alb_links=links,
                                         link_images=self.link_images,
                                         similar=ds.get_similar_info())
        self.load_view()
        #except Exception as e:
        #    print("Problem in info ready: %s" % e)


    def reload(self, artist, album_title):
        if not artist:
            return

        if self.active and artist_exceptions(artist):
            print("blank")
            self.blank_view()
            return

        # self.stack.set_visible_child_name(self.view_name)
        if self.active and (   (not self.artist or self.artist != artist)
                               or (not self.album_title or self.album_title != album_title)
        ):
            print("now loading")
            self.loading(artist, album_title)
            print("active")
            self.ds.fetch_artist_data(artist)
        else:
            print("load_view")
            self.load_view()

        self.album_title = album_title
        self.artist = artist


class ArtistDataSource(GObject.GObject):
    __gsignals__ = {
        'artist-info-ready': (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE, ())
    }

    def __init__(self, info_cache, ranking_cache):
        GObject.GObject.__init__(self)

        self.current_artist = None
        self.error = None
        cl = CoverLocale()
        lang = cl.get_locale()[:2]

        self.artist = {
            'info_en': {
                'data': None,
                'function': 'getinfo',
                'cache': info_cache,
                'signal': 'artist-info-ready',
                'parsed': False,
                'lang': 'en'
            },
            'similar_en': {
                'data': None,
                'function': 'getsimilar',
                'cache': info_cache,
                'signal': 'artist-info-ready',
                'parsed': False,
                'lang': 'en'
            }
        }

        if lang != 'en':
            self.artist['info_' + lang] = {
                'data': None,
                'function': 'getinfo',
                'cache': info_cache,
                'signal': 'artist-info-ready',
                'parsed': False,
                'lang': lang
            }
            self.artist['similar_' + lang] = {
                'data': None,
                'function': 'getsimilar',
                'cache': info_cache,
                'signal': 'artist-info-ready',
                'parsed': False,
                'lang': lang
            }

    def fetch_artist_data(self, artist):
        """
        Initiate the fetching of all artist data. Fetches artist info, similar
        artists, artist top albums and top tracks. Downloads XML files from last.fm
        and saves as parsed DOM documents in self.artist dictionary. Must be called
        before any of the get_* methods.
        """
        self.current_artist = artist
        if LastFM.user_has_account() is False:
            self.error = LASTFM_NO_ACCOUNT_ERROR
            self.emit('artist-info-ready')
            return

        self.error = None
        artist = urllib.parse.quote_plus(artist)
        self.fetched = 0
        for key, value in self.artist.items():
            print("search")
            cachekey = "lastfm:artist:%sjson:%s:%s" % (value['function'], artist, value['lang'])
            url = '%s?method=artist.%s&artist=%s&limit=10&api_key=%s&format=json&lang=%s' % (LastFM.API_URL,
                                                                                             value['function'], artist,
                                                                                             LastFM.API_KEY,
                                                                                             value['lang'])
            print("fetching %s" % url)
            value['cache'].fetch(cachekey, url, self.fetch_artist_data_cb, value)

    def fetch_artist_data_cb(self, data, category):
        if data is None:
            print("no data fetched for artist %s" % category['function'])
            return

        print(category)
        try:
            category['data'] = json.loads(data.decode('utf-8'))
            category['parsed'] = False
            self.fetched += 1
            if self.fetched == len(self.artist):
                self.emit(category['signal'])

        except Exception as e:
            print("Error parsing artist %s: %s" % (category['function'], e))
            return False

    def get_current_artist(self):
        return self.current_artist

    def get_error(self):
        return self.error

    def get_artist_images(self):
        """
        Returns tuple of image url's for small, medium, and large images.
        """
        print('get_artist_images')
        data = self.artist['info_en']['data']
        if data is None:
            return None

        if 'artist' not in data:
            return None

        print(list(data.keys()))
        images = [img['#text'] for img in data['artist'].get('image', ())]
        return images[:3]

    def get_artist_bio(self):
        """
        Returns tuple of summary and full bio
        """
        cl = CoverLocale()
        lang = cl.get_locale()[:2]

        def get_bio(lang):
            data = self.artist['info_' + lang]['data']
            if data is None:
                return None

            if not self.artist['info_' + lang]['parsed']:
                content = data['artist']['bio']['content']
                summary = data['artist']['bio']['summary']
                return summary, content

            if lang != 'en':
                return None
            else:
                return self.artist['info_' + lang]['data']['bio']


        arg = get_bio(lang)
        if not arg or arg[0] == '':
            arg = get_bio('en')

        return arg

    def get_similar_info(self):
        """
        Returns the dictionary { 'images', 'bio' }
        """
        cl = CoverLocale()
        lang = cl.get_locale()[:2]

        try:
            if not self.artist['similar_' + lang]['parsed']:
                json_artists_data = self.artist['similar_' + lang]['data']['similarartists']

                results = []
                print(json_artists_data)
                for json_artist in json_artists_data["artist"]:
                    print(json_artist)
                    name = json_artist["name"]
                    image_url = json_artist["image"][1]["#text"]
                    similarity = int(100 * float(json_artist["match"]))

                    results.append({'name': name,
                                    'image_url': image_url,
                                    'similarity': similarity})

                self.artist['similar_' + lang]['data'] = results
                self.artist['similar_' + lang]['parsed'] = True

            return self.artist['similar_' + lang]['data']
        except Exception as e:
            print("Error parsing similar_infot: %s" % e)
            return ""


    def get_artist_info(self):
        """
        Returns the dictionary { 'images', 'bio' }
        """
        cl = CoverLocale()
        lang = cl.get_locale()[:2]

        images = self.get_artist_images()

        def fill_info(lang):
            if not self.artist['info_' + lang]['parsed']:
                bio = self.get_artist_bio()
                self.artist['info_' + lang]['data'] = {'images': images,
                                                       'bio': bio}
                self.artist['info_' + lang]['parsed'] = True

            return self.artist['info_' + lang]['data']

        fill_info('en')
        return fill_info(lang)


class LinksDataSource(GObject.GObject):
    def __init__(self):
        GObject.GObject.__init__(self)
        print("init")
        self.entry = None
        self.error = None

        self.artist = None
        self.album = None

    def set_artist(self, artist):
        print("set_artist")
        self.artist = artist

    def get_artist(self):
        print("get_artist")
        return self.artist

    def set_album(self, album):
        self.album = album

    def get_album(self):
        return self.album

    def get_artist_links(self):
        """
        Return a dictionary with artist URLs to popular music databases and
        encyclopedias.
        """
        print("get_artist_links")
        artist = self.get_artist()
        if artist is not "" and artist is not None:
            wpartist = artist.replace(" ", "_")
            artist = urllib.parse.quote_plus(artist)
            artist_links = {
                "Wikipedia": "http://www.wikipedia.org/wiki/%s" % wpartist,
                "Discogs": "http://www.discogs.com/artist/%s" % artist,
                "Allmusic": "http://www.allmusic.com/search/artist/%s" % artist
            }
            return artist_links
        print("no links returned")
        print(artist)

        return None

    def get_album_links(self):
        """
        Return a dictionary with album URLs to popular music databases and
        encyclopedias.
        """
        print("get_album_links")
        album = self.get_album()
        print(album)
        if album is not None and album is not "":
            print("obtaining links")
            wpalbum = album.replace(" ", "_")
            album = urllib.parse.quote_plus(album)
            album_links = {
                "Wikipedia": "http://www.wikipedia.org/wiki/%s" % wpalbum,
                "Discogs": "http://www.discogs.com/search?type=album&q=%s&f=html" % album,
                "Allmusic": "http://allmusic.com/search/album/%s" % album
            }
            return album_links
        return None

    def get_error(self):
        if self.get_artist() is "":
            return _("No artist specified.")


class AlbumInfoView(BaseInfoView):
    def __init__(self, *args, **kwargs):
        super(AlbumInfoView, self).__init__(self, *args, **kwargs)

    def initialise(self, source, shell, plugin, stack, ds):
        super(AlbumInfoView, self).initialise(source, shell, plugin, stack, ds, "album", "covermgr_std.png")

    def connect_signals(self):
        self.ds.connect('albums-ready', self.album_list_ready)

    def loading(self, current_artist, current_album_title):
        cl = CoverLocale()
        # cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        self.loading_file = self.loading_template.render(
            artist=current_artist,
            # Translators: 'top' here means 'most popular'.  %s is replaced by the artist name.
            info=_("Loading top albums for %s") % current_artist,
            song="",
            basepath=self.basepath)
        self.webview.load_string(self.loading_file, 'text/html', 'utf-8', self.basepath)

    def load_tmpl(self):
        cl = CoverLocale()
        # cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        path = rb.find_plugin_file(self.plugin, 'tmpl/album-tmpl.html')
        empty_path = rb.find_plugin_file(self.plugin, 'tmpl/album_empty-tmpl.html')
        self.loading_path = rb.find_plugin_file(self.plugin, 'tmpl/loading.html')
        self.album_template = Template(filename=path)
        self.loading_template = Template(filename=self.loading_path)
        self.empty_template = Template(filename=empty_path)
        self.styles = self.basepath + '/tmpl/artistmain.css'

    def album_list_ready(self, ds):
        print("album_list_ready")
        cl = CoverLocale()
        # cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        self.file = self.album_template.render(error=ds.get_error(),
                                               albums=ds.get_top_albums(),
                                               artist=ds.get_artist(),
                                               datasource=lastfm_datasource_link(self.basepath),
                                               stylesheet=self.styles)
        self.load_view()

    def reload(self, artist, album_title):
        print("reload")
        if not artist:
            return

        if self.active and artist_exceptions(artist):
            print("blank")
            self.blank_view()
            return

        if self.active and (not self.artist or artist != self.artist):
            self.loading(artist, album_title)
            self.ds.fetch_album_list(artist)
        else:
            self.load_view()

        self.album_title = album_title
        self.artist = artist


class AlbumDataSource(GObject.GObject):
    __gsignals__ = {
        'albums-ready': (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE, ())
    }

    def __init__(self, info_cache, ranking_cache):
        GObject.GObject.__init__(self)
        self.albums = None
        self.error = None
        self.artist = None
        self.max_albums_fetched = 8
        self.fetching = 0
        self.info_cache = info_cache
        self.ranking_cache = ranking_cache

    def get_artist(self):
        return self.artist

    def get_error(self):
        return self.error

    def fetch_album_list(self, artist):
        if LastFM.user_has_account() is False:
            self.error = LASTFM_NO_ACCOUNT_ERROR
            self.emit('albums-ready')
            return

        cl = CoverLocale()
        lang = cl.get_locale()[:2]

        self.artist = artist
        qartist = urllib.parse.quote_plus(artist)
        self.error = None
        url = "%s?method=artist.gettopalbums&artist=%s&api_key=%s&format=json&lang=%s" % (
            LastFM.API_URL, qartist, LastFM.API_KEY, lang)
        print(url)
        cachekey = 'lastfm:artist:gettopalbumsjson:%s' % qartist
        self.ranking_cache.fetch(cachekey, url, self.parse_album_list, artist)

    def parse_album_list(self, data, artist):
        if data is None:
            print("Nothing fetched for %s top albums" % artist)
            return False

        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception as e:
            print("Error parsing album list: %s" % e)
            return False

        self.error = parsed.get('error')
        if self.error:
            self.emit('albums-ready')
            return False

        try:
            albums = parsed['topalbums'].get('album', [])[:self.max_albums_fetched]
        except:
            albums = []

        if len(albums) == 0:
            self.error = "No albums found for %s" % artist
            self.emit('albums-ready')
            return True
        print(albums)
        self.albums = []
        print("max number of albums to process")
        print(len(albums))
        # albums = parsed['topalbums'].get('album', [])[:self.max_albums_fetched]
        self.fetching = len(albums)

        for i, a in enumerate(albums):
            try:
                images = [img['#text'] for img in a.get('image', [])]
                self.albums.append({'title': a.get('name'), 'images': images[:3]})
                self.fetch_album_info(artist, a.get('name'), i)
            except:
                pass

        return True

    def get_top_albums(self):
        return self.albums

    def fetch_album_info(self, artist, album, index):
        print('start fetch_album_info')
        cl = CoverLocale()
        lang = cl.get_locale()[:2]
        qartist = urllib.parse.quote_plus(artist)
        qalbum = urllib.parse.quote_plus(album)
        self.fetched = 2

        def fetch_information(lang):
            cachekey = "lastfm:album:getinfojson:%s:%s:%s" % (qartist, qalbum, lang)
            url = "%s?method=album.getinfo&artist=%s&album=%s&api_key=%s&format=json&lang=%s" % (
                LastFM.API_URL, qartist, qalbum, LastFM.API_KEY, lang)
            print(url)

            self.info_cache.fetch(cachekey, url, self.parse_album_info, album, index, lang)

        self.album_data = {}
        fetch_information('en')
        fetch_information(lang)
        print('end fetch_album_info')

    def parse_album_info(self, data, album, index, lang):
        print('parse_album_info %s' % lang)
        self.fetched = self.fetched - 1
        self.album_data[lang] = data
        if self.fetched > 0:
            print('return %d' % self.fetched)
            return

        cl = CoverLocale()
        lang = cl.get_locale()[:2]
        rv = True
        try:
            print('decoding')
            parsed = json.loads(self.album_data[lang].decode('utf-8'))
            print('decoded')
            self.albums[index]['id'] = parsed['album']['id']
            for k in ('releasedate', 'summary'):
                self.albums[index][k] = parsed['album'].get(k)
            tracklist = []
            print(parsed['album'])
            tracks = parsed['album']['tracks'].get('track', [])
            for i, t in enumerate(tracks):
                title = t['name']
                duration = int(t['duration'])
                tracklist.append((i, title, duration))
            self.albums[index]['tracklist'] = tracklist
            self.albums[index]['duration'] = sum([t[2] for t in tracklist])

            if 'wiki' in parsed['album']:
                self.albums[index]['wiki-summary'] = parsed['album']['wiki']['summary']
                self.albums[index]['wiki-content'] = parsed['album']['wiki']['content']
            elif lang != 'en':
                parsed = json.loads(self.album_data['en'].decode('utf-8'))
                if 'wiki' in parsed['album']:
                    self.albums[index]['wiki-summary'] = parsed['album']['wiki']['summary']
                    self.albums[index]['wiki-content'] = parsed['album']['wiki']['content']
        except Exception as e:
            print("Error parsing album tracklist: %s" % e)
            rv = False

        self.fetching -= 1
        print("%s albums left to process" % self.fetching)
        if self.fetching == 0:
            self.emit('albums-ready')
        return rv


class EchoArtistInfoView(BaseInfoView):
    def __init__(self, *args, **kwargs):
        super(EchoArtistInfoView, self).__init__(self, *args, **kwargs)

    def initialise(self, source, shell, plugin, stack, ds, link_ds):
        super(EchoArtistInfoView, self).initialise(source, shell, plugin, stack, ds, "echoartist",
                                                   "echonest_minilogo.png")

        self.link_ds = link_ds

    def load_tmpl(self):
        cl = CoverLocale()
        # cl.switch_locale(cl.Locale.LOCALE_DOMAIN)

        path = rb.find_plugin_file(self.plugin, 'tmpl/echoartist-tmpl.html')
        empty_path = rb.find_plugin_file(self.plugin, 'tmpl/artist_empty-tmpl.html')
        loading_path = rb.find_plugin_file(self.plugin, 'tmpl/loading.html')
        self.template = Template(filename=path)
        self.loading_template = Template(filename=loading_path)
        self.empty_template = Template(filename=empty_path)
        self.styles = self.basepath + '/tmpl/artistmain.css'
        print(lastfm_datasource_link(self.basepath))

    def connect_signals(self):
        self.air_id = self.ds.connect('artist-info-ready', self.artist_info_ready)

    def artist_info_ready(self, ds):
        # Can only be called after the artist-info-ready signal has fired.
        # If called any other time, the behavior is undefined
        # try:
        link_album = self.link_ds.get_album()
        if not link_album:
            link_album = ""

        links = self.link_ds.get_album_links()
        if not links:
            links = {}

        print(ds.get_current_artist())
        print(ds.get_error())
        print(self.ds.get_artist_bio())
        print(self.styles)
        print(link_album)
        print(self.link_ds.get_artist_links())
        print(links)
        print(self.link_images)
        print(ds.get_attribution())
        self.file = self.template.render(artist=ds.get_current_artist(),
                                         error=ds.get_error(),
                                         bio=self.ds.get_artist_bio(),
                                         stylesheet=self.styles,
                                         album=link_album,
                                         art_links=self.link_ds.get_artist_links(),
                                         alb_links=links,
                                         link_images=self.link_images,
                                         datasource=ds.get_attribution())
        self.load_view()
        #except Exception as e:
        #    print("Problem in info ready: %s" % e)

    def reload(self, artist, album_title):
        if not artist:
            return

        if self.active and artist_exceptions(artist):
            print("blank")
            self.blank_view()
            return

        # self.stack.set_visible_child_name(self.view_name)
        if self.active and (   (not self.artist or self.artist != artist)
                               or (not self.album_title or self.album_title != album_title)
        ):
            print("now loading")
            self.loading(artist, album_title)
            print("active")
            self.ds.fetch_artist_data(artist)
        else:
            print("load_view")
            self.load_view()

        self.album_title = album_title
        self.artist = artist


class EchoArtistDataSource(GObject.GObject):
    __gsignals__ = {
        'artist-info-ready': (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE, ())
    }

    def __init__(self, info_cache, ranking_cache):
        GObject.GObject.__init__(self)

        self.current_artist = None
        self.error = None
        self.artist = {
            'info': {
                'data': None,
                'cache': info_cache,
                'signal': 'artist-info-ready',
                'parsed': False
            }
        }

    def fetch_artist_data(self, artist):
        """
        Initiate the fetching of all artist data. Fetches artist info, similar
        artists, artist top albums and top tracks. Downloads XML files from last.fm
        and saves as parsed DOM documents in self.artist dictionary. Must be called
        before any of the get_* methods.
        """
        self.current_artist = artist

        self.error = None
        artist = urllib.parse.quote_plus(artist)
        self.fetched = 0
        for key, value in self.artist.items():
            print("search")
            cachekey = "echonest:artist:json:%s" % (artist)
            api_url = "http://developer.echonest.com/api/v4/"
            api_key = "N685TONJGZSHBDZMP"
            url = '%sartist/biographies?api_key=%s&name=%s&format=json&results=1&start=0' % (api_url,
                                                                                             api_key, artist)

            print("fetching %s" % url)
            value['cache'].fetch(cachekey, url, self.fetch_artist_data_cb, value)

    def fetch_artist_data_cb(self, data, category):
        if data is None:
            print("no data fetched for artist")
            return

        print(category)
        try:
            category['data'] = json.loads(data.decode('utf-8'))
            category['parsed'] = False
            self.fetched += 1
            if self.fetched == len(self.artist):
                self.emit(category['signal'])

        except Exception as e:
            print("Error parsing artist %s" % e)
            return False

    def get_current_artist(self):
        return self.current_artist

    def get_error(self):
        return self.error

    def get_attribution(self):
        print('get_attribution')
        data = self.artist['info']['data']
        if data is None:
            print('nothing here')
            return None
        content = ""

        if not self.artist['info']['parsed']:
            print(data)
            url = data['response']['biographies'][0]['url']
            site = data['response']['biographies'][0]['site']
            print(url)
            print(site)
            return "<a href='%s'>%s</a>" % (url, site)

        return content

    def get_artist_bio(self):
        """
        Returns tuple of summary and full bio
        """
        data = self.artist['info']['data']
        if data is None:
            print('nothing here')
            return None

        if not self.artist['info']['parsed']:
            print(data)
            content = data['response']['biographies'][0]['text']
            return content

        return self.artist['info']['data']['response']['biographies'][0]['text']

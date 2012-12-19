# ###################################################
# Copyright (C) 2010 The Unknown Horizons Team
# team@unknown-horizons.org
# This file is part of Unknown Horizons.
#
# Unknown Horizons is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the
# Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
# ###################################################

import re
import horizons.globals
from fife import fife

from horizons.entities import Entities
from horizons.util.living import livingProperty, LivingObject
from horizons.util.pychanchildfinder import PychanChildFinder
from horizons.util.python.callback import Callback
from horizons.gui.mousetools import BuildingTool
from horizons.gui.tabs import TabWidget, BuildTab, DiplomacyTab, SelectMultiTab
from horizons.gui.widgets.imagebutton import OkButton, CancelButton
from horizons.gui.widgets.messagewidget import MessageWidget
from horizons.gui.widgets.minimap import Minimap
from horizons.gui.widgets.logbook import LogBook
from horizons.gui.widgets.playersoverview import PlayersOverview
from horizons.gui.widgets.playerssettlements import PlayersSettlements
from horizons.gui.widgets.resourceoverviewbar import ResourceOverviewBar
from horizons.gui.widgets.playersships import PlayersShips
from horizons.extscheduler import ExtScheduler
from horizons.gui.util import LazyWidgetsDict
from horizons.constants import BUILDINGS, GUI, GAME_SPEED, VERSION
from horizons.command.uioptions import RenameObject
from horizons.command.misc import Chat
from horizons.command.game import SpeedDownCommand, SpeedUpCommand, TogglePauseCommand
from horizons.gui.tabs.tabinterface import TabInterface
from horizons.gui.tabs import MainSquareOverviewTab
from horizons.gui.keylisteners import KeyConfig
from horizons.component.namedcomponent import SettlementNameComponent, NamedComponent
from horizons.component.ambientsoundcomponent import AmbientSoundComponent
from horizons.component.selectablecomponent import SelectableComponent
from horizons.messaging import (SettlerUpdate, SettlerInhabitantsChanged, ResourceBarResize,
                                HoverSettlementChanged, TabWidgetChanged, SpeedChanged)
from horizons.util.lastactiveplayersettlementmanager import LastActivePlayerSettlementManager

class IngameGui(LivingObject):
	"""Class handling all the ingame gui events.
	Assumes that only 1 instance is used (class variables)"""

	gui = livingProperty()
	tabwidgets = livingProperty()
	message_widget = livingProperty()
	minimap = livingProperty()

	styles = {
		'city_info' : 'resource_bar',
		'change_name' : 'book',
		'save_map' : 'book',
		'chat' : 'book',
	}

	def __init__(self, session, gui):
		super(IngameGui, self).__init__()
		self.session = session
		assert isinstance(self.session, horizons.session.Session)
		self.main_gui = gui
		self.main_widget = None
		self.tabwidgets = {}
		self.settlement = None
		self.resource_source = None
		self.resources_needed, self.resources_usable = {}, {}
		self._old_menu = None

		self.widgets = LazyWidgetsDict(self.styles)

		self.cityinfo = self.widgets['city_info']
		self.cityinfo.child_finder = PychanChildFinder(self.cityinfo)

		self.logbook = LogBook(self.session)
		self.message_widget = MessageWidget(self.session)
		self.players_overview = PlayersOverview(self.session)
		self.players_settlements = PlayersSettlements(self.session)
		self.players_ships = PlayersShips(self.session)
		self.chat_dialog = ChatDialog(self.main_gui, self.session, self.widgets['chat'])
		self.change_name_dialog = ChangeNameDialog(self.main_gui, self.session, self.widgets['change_name'])

		# self.widgets['minimap'] is the guichan gui around the actual minimap,
		# which is saved in self.minimap
		minimap = self.widgets['minimap']
		minimap.position_technique = "right+0:top+0"

		icon = minimap.findChild(name="minimap")
		self.minimap = Minimap(icon,
		                       targetrenderer=horizons.globals.fife.targetrenderer,
		                       imagemanager=horizons.globals.fife.imagemanager,
		                       session=self.session,
		                       view=self.session.view)

		def speed_up():
			SpeedUpCommand().execute(self.session)

		def speed_down():
			SpeedDownCommand().execute(self.session)

		minimap.mapEvents({
			'zoomIn' : self.session.view.zoom_in,
			'zoomOut' : self.session.view.zoom_out,
			'rotateRight' : Callback.ChainedCallbacks(self.session.view.rotate_right, self.minimap.rotate_right),
			'rotateLeft' : Callback.ChainedCallbacks(self.session.view.rotate_left, self.minimap.rotate_left),
			'speedUp' : speed_up,
			'speedDown' : speed_down,
			'destroy_tool' : self.session.toggle_destroy_tool,
			'build' : self.show_build_menu,
			'diplomacyButton' : self.show_diplomacy_menu,
			'gameMenuButton' : self.main_gui.toggle_pause,
			'logbook' : self.logbook.toggle_visibility
		})
		minimap.show()
		#minimap.position_technique = "right+15:top+153"

		self.widgets['tooltip'].hide()

		self.resource_overview = ResourceOverviewBar(self.session)
		ResourceBarResize.subscribe(self._on_resourcebar_resize)

		# Register for messages
		SettlerUpdate.subscribe(self._on_settler_level_change)
		SettlerInhabitantsChanged.subscribe(self._on_settler_inhabitant_change)
		HoverSettlementChanged.subscribe(self._cityinfo_set)
		SpeedChanged.subscribe(self._on_speed_changed)

		self._display_speed(self.session.timer.ticks_per_second)

	def _on_resourcebar_resize(self, message):
		self._update_cityinfo_position()

	def end(self):
		self.widgets['minimap'].mapEvents({
			'zoomIn' : None,
			'zoomOut' : None,
			'rotateRight' : None,
			'rotateLeft': None,

			'destroy_tool' : None,
			'build' : None,
			'diplomacyButton' : None,
			'gameMenuButton' : None
		})

		for w in self.widgets.itervalues():
			if w.parent is None:
				w.hide()
		self.message_widget = None
		self.tabwidgets = None
		self.minimap = None
		self.resource_overview.end()
		self.resource_overview = None
		self.hide_menu()
		SettlerUpdate.unsubscribe(self._on_settler_level_change)
		ResourceBarResize.unsubscribe(self._on_resourcebar_resize)
		HoverSettlementChanged.unsubscribe(self._cityinfo_set)
		SettlerInhabitantsChanged.unsubscribe(self._on_settler_inhabitant_change)
		SpeedChanged.unsubscribe(self._on_speed_changed)

		super(IngameGui, self).end()

	def _cityinfo_set(self, message):
		"""Sets the city name at top center of screen.

		Show/Hide is handled automatically
		To hide cityname, set name to ''
		@param message: HoverSettlementChanged message
		"""
		settlement = message.settlement
		old_was_player_settlement = False
		if self.settlement is not None:
			self.settlement.remove_change_listener(self.update_settlement)
			old_was_player_settlement = self.settlement.owner.is_local_player

		# save reference to new "current" settlement in self.settlement
		self.settlement = settlement

		if settlement is None: # we want to hide the widget now (but perhaps delayed).
			if old_was_player_settlement:
				# After scrolling away from settlement, leave name on screen for some
				# seconds. Players can still click on it to rename the settlement now.
				ExtScheduler().add_new_object(self.cityinfo.hide, self,
				      run_in=GUI.CITYINFO_UPDATE_DELAY)
				#TODO 'click to rename' tooltip of cityinfo can stay visible in
				# certain cases if cityinfo gets hidden in tooltip delay buffer.
			else:
				# hovered settlement of other player, simply hide the widget
				self.cityinfo.hide()

		else:# do not hide if settlement is hovered and a hide was previously scheduled
			ExtScheduler().rem_call(self, self.cityinfo.hide)

			self.update_settlement() # calls show()
			settlement.add_change_listener(self.update_settlement)

	def _on_settler_inhabitant_change(self, message):
		assert isinstance(message, SettlerInhabitantsChanged)
		foundlabel = self.cityinfo.child_finder('city_inhabitants')
		old_amount = int(foundlabel.text) if foundlabel.text else 0
		foundlabel.text = u' {amount:>4d}'.format(amount=old_amount+message.change)
		foundlabel.resizeToContent()

	def update_settlement(self):
		city_name_label = self.cityinfo.child_finder('city_name')
		if self.settlement.owner.is_local_player: # allow name changes
			# Update settlement on the resource overview to make sure it
			# is setup correctly for the coming calculations
			self.resource_overview.set_inventory_instance(self.settlement)
			cb = Callback(self.show_change_name_dialog, self.settlement)
			helptext = _("Click to change the name of your settlement")
			city_name_label.enable_cursor_change_on_hover()
		else: # no name changes
			cb = lambda : AmbientSoundComponent.play_special('error')
			helptext = u""
			city_name_label.disable_cursor_change_on_hover()
		self.cityinfo.mapEvents({
			'city_name': cb
		})
		city_name_label.helptext = helptext

		foundlabel = self.cityinfo.child_finder('owner_emblem')
		foundlabel.image = 'content/gui/images/tabwidget/emblems/emblem_%s.png' % (self.settlement.owner.color.name)
		foundlabel.helptext = self.settlement.owner.name

		foundlabel = self.cityinfo.child_finder('city_name')
		foundlabel.text = self.settlement.get_component(SettlementNameComponent).name
		foundlabel.resizeToContent()

		foundlabel = self.cityinfo.child_finder('city_inhabitants')
		foundlabel.text = u' {amount:>4d}'.format(amount=self.settlement.inhabitants)
		foundlabel.resizeToContent()

		self._update_cityinfo_position()

	def _update_cityinfo_position(self):
		""" Places cityinfo widget depending on resource bar dimensions.

		For a normal-sized resource bar and reasonably large resolution:
		* determine resource bar length (includes gold)
		* determine empty horizontal space between resbar end and minimap start
		* display cityinfo centered in that area if it is sufficiently large

		If too close to the minimap (cityinfo larger than length of this empty space)
		move cityinfo centered to very upper screen edge. Looks bad, works usually.
		In this case, the resbar is redrawn to put the cityinfo "behind" it visually.
		"""
		width = horizons.globals.fife.engine_settings.getScreenWidth()
		resbar = self.resource_overview.get_size()
		is_foreign = (self.settlement.owner != self.session.world.player)
		blocked = self.cityinfo.size[0] + int(1.5*self.minimap.get_size()[1])
		# minimap[1] returns width! Use 1.5*width because of the GUI around it

		if is_foreign: # other player, no resbar exists
			self.cityinfo.pos = ('center', 'top')
			xoff = 0
			yoff = 19
		elif blocked < width < resbar[0] + blocked: # large resbar / small resolution
			self.cityinfo.pos = ('center', 'top')
			xoff = 0
			yoff = 0 # upper screen edge
		else:
			self.cityinfo.pos = ('left', 'top')
			xoff = resbar[0] + (width - blocked - resbar[0]) // 2
			yoff = 24

		self.cityinfo.offset = (xoff, yoff)
		self.cityinfo.position_technique = "{pos[0]}{off[0]:+d}:{pos[1]}{off[1]:+d}".format(
				pos=self.cityinfo.pos,
				off=self.cityinfo.offset)
		self.cityinfo.hide()
		self.cityinfo.show()

	def minimap_to_front(self):
		"""Make sure the full right top gui is visible and not covered by some dialog"""
		self.widgets['minimap'].hide()
		self.widgets['minimap'].show()

	def show_diplomacy_menu(self):
		# check if the menu is already shown
		if getattr(self.get_cur_menu(), 'name', None) == "diplomacy_widget":
			self.hide_menu()
			return

		if not DiplomacyTab.is_useable(self.session.world):
			self.main_gui.show_popup(_("No diplomacy possible"),
			                         _("Cannot do diplomacy as there are no other players."))
			return

		tab = DiplomacyTab(self, self.session.world)
		self.show_menu(tab)

	def show_multi_select_tab(self):
		tab = TabWidget(self, tabs=[SelectMultiTab(self.session)], name='select_multi')
		self.show_menu(tab)

	def show_build_menu(self, update=False):
		"""
		@param update: set when build possiblities change (e.g. after settler upgrade)
		"""
		# check if build menu is already shown
		if hasattr(self.get_cur_menu(), 'name') and self.get_cur_menu().name == "build_menu_tab_widget":
			self.hide_menu()

			if not update: # this was only a toggle call, don't reshow
				return

		self.session.set_cursor() # set default cursor for build menu
		self.deselect_all()

		if not any( settlement.owner.is_local_player for settlement in self.session.world.settlements):
			# player has not built any settlements yet. Accessing the build menu at such a point
			# indicates a mistake in the mental model of the user. Display a hint.
			tab = TabWidget(self, tabs=[ TabInterface(widget="buildtab_no_settlement.xml") ])
		else:
			btabs = BuildTab.create_tabs(self.session, self._build)
			tab = TabWidget(self, tabs=btabs, name="build_menu_tab_widget",
											active_tab=BuildTab.last_active_build_tab)
		self.show_menu(tab)

	def deselect_all(self):
		for instance in self.session.selected_instances:
			instance.get_component(SelectableComponent).deselect()
		self.session.selected_instances.clear()

	def _build(self, building_id, unit=None):
		"""Calls the games buildingtool class for the building_id.
		@param building_id: int with the building id that is to be built.
		@param unit: weakref to the unit, that builds (e.g. ship for warehouse)"""
		self.hide_menu()
		self.deselect_all()
		cls = Entities.buildings[building_id]
		if hasattr(cls, 'show_build_menu'):
			cls.show_build_menu()
		self.session.set_cursor('building', cls, None if unit is None else unit())

	def toggle_road_tool(self):
		if not isinstance(self.session.cursor, BuildingTool) or self.session.cursor._class.id != BUILDINGS.TRAIL:
			self._build(BUILDINGS.TRAIL)
		else:
			self.session.set_cursor()

	def _get_menu_object(self, menu):
		"""Returns pychan object if menu is a string, else returns menu
		@param menu: str with the guiname or pychan object.
		"""
		if isinstance(menu, str):
			menu = self.widgets[menu]
		return menu

	def get_cur_menu(self):
		"""Returns menu that is currently displayed"""
		return self._old_menu

	def show_menu(self, menu):
		"""Shows a menu
		@param menu: str with the guiname or pychan object.
		"""
		if self._old_menu is not None:
			if hasattr(self._old_menu, "remove_remove_listener"):
				self._old_menu.remove_remove_listener( Callback(self.show_menu, None) )
			self._old_menu.hide()

		self._old_menu = self._get_menu_object(menu)
		if self._old_menu is not None:
			if hasattr(self._old_menu, "add_remove_listener"):
				self._old_menu.add_remove_listener( Callback(self.show_menu, None) )
			self._old_menu.show()
			self.minimap_to_front()

		TabWidgetChanged.broadcast(self)

	def hide_menu(self):
		self.show_menu(None)

	def toggle_menu(self, menu):
		"""Shows a menu or hides it if it is already displayed.
		@param menu: parameter supported by show_menu().
		"""
		if self.get_cur_menu() == self._get_menu_object(menu):
			self.hide_menu()
		else:
			self.show_menu(menu)

	def save(self, db):
		self.message_widget.save(db)
		self.logbook.save(db)
		self.resource_overview.save(db)

	def load(self, db):
		self.message_widget.load(db)
		self.logbook.load(db)
		self.resource_overview.load(db)

		cur_settlement = LastActivePlayerSettlementManager().get_current_settlement()
		self._cityinfo_set( HoverSettlementChanged(self, cur_settlement) )

		self.minimap.draw() # update minimap to new world

	def show_change_name_dialog(self, instance):
		"""Shows a dialog where the user can change the name of an object."""
		self.change_name_dialog.show(instance)

	def show_save_map_dialog(self):
		"""Shows a dialog where the user can set the name of the saved map."""
		events = {
			OkButton.DEFAULT_NAME: self.save_map,
			CancelButton.DEFAULT_NAME: self._hide_save_map_dialog
		}
		self.main_gui.on_escape = self._hide_save_map_dialog
		dialog = self.widgets['save_map']
		name = dialog.findChild(name='map_name')
		name.text = u''
		dialog.mapEvents(events)
		name.capture(Callback(self.save_map))
		dialog.show()
		name.requestFocus()

	def _hide_save_map_dialog(self):
		"""Closes the map saving dialog."""
		self.main_gui.on_escape = self.main_gui.toggle_pause
		self.widgets['save_map'].hide()

	def save_map(self):
		"""Saves the map and hides the dialog."""
		name = self.widgets['save_map'].collectData('map_name')
		if re.match('^[a-zA-Z0-9_-]+$', name):
			self.session.save_map(name)
			self._hide_save_map_dialog()
		else:
			#xgettext:python-format
			message = _('Valid map names are in the following form: {expression}').format(expression='[a-zA-Z0-9_-]+')
			#xgettext:python-format
			advice = _('Try a name that only contains letters and numbers.')
			self.session.gui.show_error_popup(_('Error'), message, advice)

	def on_escape(self):
		if self.main_widget:
			self.main_widget.hide()
		else:
			return False
		return True

	def on_switch_main_widget(self, widget):
		"""The main widget has been switched to the given one (possibly None)."""
		if self.main_widget and self.main_widget != widget: # close the old one if it exists
			old_main_widget = self.main_widget
			self.main_widget = None
			old_main_widget.hide()
		self.main_widget = widget

	def _on_settler_level_change(self, message):
		"""Gets called when the player changes"""
		if message.sender.owner.is_local_player:
			menu = self.get_cur_menu()
			if hasattr(menu, "name") and menu.name == "build_menu_tab_widget":
				# player changed and build menu is currently displayed
				self.show_build_menu(update=True)

			# TODO: Use a better measure then first tab
			# Quite fragile, makes sure the tablist in the mainsquare menu is updated
			if hasattr(menu, '_tabs') and isinstance(menu._tabs[0], MainSquareOverviewTab):
				instance = list(self.session.selected_instances)[0]
				instance.get_component(SelectableComponent).show_menu(jump_to_tabclass=type(menu.current_tab))

	def _on_speed_changed(self, message):
		self._display_speed(message.new)

	def _display_speed(self, tps):
		text = u''
		up_icon = self.widgets['minimap'].findChild(name='speedUp')
		down_icon = self.widgets['minimap'].findChild(name='speedDown')
		if tps == 0: # pause
			text = u'0x'
			up_icon.set_inactive()
			down_icon.set_inactive()
		else:
			if tps != GAME_SPEED.TICKS_PER_SECOND:
				text = unicode("%1gx" % (tps * 1.0/GAME_SPEED.TICKS_PER_SECOND))
				#%1g: displays 0.5x, but 2x instead of 2.0x
			index = GAME_SPEED.TICK_RATES.index(tps)
			if index + 1 >= len(GAME_SPEED.TICK_RATES):
				up_icon.set_inactive()
			else:
				up_icon.set_active()
			if index > 0:
				down_icon.set_active()
			else:
				down_icon.set_inactive()

		wdg = self.widgets['minimap'].findChild(name="speed_text")
		wdg.text = text
		wdg.resizeToContent()
		self.widgets['minimap'].show()

	def on_key_press(self, action, evt):
		"""Handle a key press in-game.

		Returns True if the key was acted upon.
		"""
		_Actions = KeyConfig._Actions
		keyval = evt.getKey().getValue()

		if action == _Actions.GRID:
			gridrenderer = self.session.view.renderer['GridRenderer']
			gridrenderer.setEnabled( not gridrenderer.isEnabled() )
		elif action == _Actions.COORD_TOOLTIP:
			self.session.coordinates_tooltip.toggle()
		elif action == _Actions.DESTROY_TOOL:
			self.session.toggle_destroy_tool()
		elif action == _Actions.REMOVE_SELECTED:
			self.session.remove_selected()
		elif action == _Actions.ROAD_TOOL:
			self.toggle_road_tool()
		elif action == _Actions.SPEED_UP:
			SpeedUpCommand().execute(self.session)
		elif action == _Actions.SPEED_DOWN:
			SpeedDownCommand().execute(self.session)
		elif action == _Actions.PAUSE:
			TogglePauseCommand().execute(self.session)
		elif action == _Actions.PLAYERS_OVERVIEW:
			self.logbook.toggle_stats_visibility(widget='players')
		elif action == _Actions.SETTLEMENTS_OVERVIEW:
			self.logbook.toggle_stats_visibility(widget='settlements')
		elif action == _Actions.SHIPS_OVERVIEW:
			self.logbook.toggle_stats_visibility(widget='ships')
		elif action == _Actions.LOGBOOK:
			self.logbook.toggle_visibility()
		elif action == _Actions.DEBUG and VERSION.IS_DEV_VERSION:
			import pdb; pdb.set_trace()
		elif action == _Actions.BUILD_TOOL:
			self.show_build_menu()
		elif action == _Actions.ROTATE_RIGHT:
			if hasattr(self.session.cursor, "rotate_right"):
				# used in e.g. build preview to rotate building instead of map
				self.session.cursor.rotate_right()
			else:
				self.session.view.rotate_right()
				self.minimap.rotate_right()
		elif action == _Actions.ROTATE_LEFT:
			if hasattr(self.session.cursor, "rotate_left"):
				self.session.cursor.rotate_left()
			else:
				self.session.view.rotate_left()
				self.minimap.rotate_left()
		elif action == _Actions.CHAT:
			self.chat_dialog.show()
		elif action == _Actions.TRANSLUCENCY:
			self.session.world.toggle_translucency()
		elif action == _Actions.TILE_OWNER_HIGHLIGHT:
			self.session.world.toggle_owner_highlight()
		elif keyval in (fife.Key.NUM_0, fife.Key.NUM_1, fife.Key.NUM_2, fife.Key.NUM_3, fife.Key.NUM_4,
		                fife.Key.NUM_5, fife.Key.NUM_6, fife.Key.NUM_7, fife.Key.NUM_8, fife.Key.NUM_9):
			num = int(keyval - fife.Key.NUM_0)
			if evt.isControlPressed():
				# create new group (only consider units owned by the player)
				self.session.selection_groups[num] = \
				    set(filter(lambda unit : unit.owner.is_local_player,
				               self.session.selected_instances))
				# drop units of the new group from all other groups
				for group in self.session.selection_groups:
					if group is not self.session.selection_groups[num]:
						group -= self.session.selection_groups[num]
			else:
				# deselect
				# we need to make sure to have a cursor capable of selection (for apply_select())
				# this handles deselection implicitly in the destructor
				self.session.set_cursor('selection')

				# apply new selection
				for instance in self.session.selection_groups[num]:
					instance.get_component(SelectableComponent).select(reset_cam=True)
				# assign copy since it will be randomly changed, the unit should only be changed on ctrl-events
				self.session.selected_instances = self.session.selection_groups[num].copy()
				# show menu depending on the entities selected
				if self.session.selected_instances:
					self.session.cursor.apply_select()
				else:
					# nothing is selected here, we need to hide the menu since apply_select doesn't handle that case
					self.show_menu(None)
		elif action == _Actions.QUICKSAVE:
			self.session.quicksave() # load is only handled by the MainListener
		elif action == _Actions.PIPETTE:
			# copy mode: pipette tool
			self.session.toggle_cursor('pipette')
		elif action == _Actions.HEALTH_BAR:
			# shows health bar of every instance with an health component
			self.session.world.toggle_health_for_all_health_instances()
		elif action == _Actions.SHOW_SELECTED:
			if self.session.selected_instances:
				# scroll to first one, we can never guarantee to display all selected units
				instance = iter(self.session.selected_instances).next()
				self.session.view.center( * instance.position.center.to_tuple())
				for instance in self.session.selected_instances:
					if hasattr(instance, "path") and instance.owner.is_local_player:
						self.minimap.show_unit_path(instance)
		else:
			return False

		return True


class ChatDialog(object):
	"""Allow player to send messages to other players."""

	def __init__(self, main_gui, session, widget):
		self._main_gui = main_gui
		self._session = session
		self._widget = widget

		events = {
			OkButton.DEFAULT_NAME: self._do_chat,
			CancelButton.DEFAULT_NAME: self.hide
		}
		self._widget.mapEvents(events)

		def forward_escape(event):
			# the textfield will eat everything, even control events
			if event.getKey().getValue() == fife.Key.ESCAPE:
				self._main_gui.on_escape()

		self._widget.findChild(name="msg").capture(forward_escape, "keyPressed")
		self._widget.findChild(name="msg").capture(self._do_chat)

	def show(self):
		self._main_gui.on_escape = self.hide
		self._widget.show()
		self._widget.findChild(name="msg").requestFocus()

	def hide(self):
		self._main_gui.on_escape = self._main_gui.toggle_pause
		self._widget.hide()

	def _do_chat(self):
		"""Actually initiates chatting and hides the dialog"""
		msg = self._widget.findChild(name="msg").text
		Chat(msg).execute(self._session)
		self._widget.findChild(name="msg").text = u''
		self.hide()


class ChangeNameDialog(object):
	"""Shows a dialog where the user can change the name of a NamedComponent."""

	def __init__(self, main_gui, session, widget):
		self._main_gui = main_gui
		self._session = session
		self._widget = widget

		self._widget.mapEvents({CancelButton.DEFAULT_NAME: self.hide})

		def forward_escape(event):
			# the textfield will eat everything, even control events
			if event.getKey().getValue() == fife.Key.ESCAPE:
				self._main_gui.on_escape()

		self._widget.findChild(name="new_name").capture(forward_escape, "keyPressed")

	def show(self, instance):
		self._main_gui.on_escape = self.hide

		cb = Callback(self._do_change_name, instance)
		self._widget.mapEvents({OkButton.DEFAULT_NAME: cb})
		self._widget.findChild(name="new_name").capture(cb)

		oldname = self._widget.findChild(name='old_name')
		oldname.text = instance.get_component(NamedComponent).name

		self._widget.show()
		self._widget.findChild(name="new_name").requestFocus()

	def hide(self):
		self._main_gui.on_escape = self._main_gui.toggle_pause
		self._widget.hide()

	def _do_change_name(self, instance):
		new_name = self._widget.collectData('new_name')
		self._widget.findChild(name='new_name').text = u''

		if not new_name or not new_name.isspace():
			# different namedcomponent classes share the name
			RenameObject(instance.get_component_by_name(NamedComponent.NAME), new_name).execute(self._session)

		self.hide()

# Packages/User/breadcrumb_panel.py
#
# Breadcrumb Panel for Sublime Text
#
# ## Why this exists
#
# In deeply indented code it is easy to lose track of which class, function or control block you
# are inside. Breadcrumb Panel gives you that context in a persistent bottom panel so it is always
# visible, without popups or phantoms. It works across languages by following indentation rather
# than doing full parsing.
#
# ## What you get
#
# - A live, caret-aware breadcrumb list of the lines that caused the current indentation.
# - Click a breadcrumb line to jump the cursor to that line.
# - Graceful messages: “Multiple contexts” for multi-cursor, “No indent” at top level.
# - Dormant when hidden. No background work while the panel is not shown.
#
# ## How to use it
#
# 1) Bind a key and toggle the panel:
#       { "keys": ["ctrl+alt+b"], "command": "toggle_breadcrumb_panel" }
#    The panel appears as:  output.breadcrumb_panel
# 2) Move the caret. The panel updates with outermost context at the top and nearest at the bottom.
# 3) Click a line in the panel to navigate to it in the active view.
#
# ## Useful commands
#
# - toggle_breadcrumb_panel           Show or hide the panel. Also enables or disables the listener.
# - toggle_breadcrumb_panel_debug     Toggle verbose logging to the Sublime console.
#
# Optional settings (Packages/User/Breadcrumb Panel.sublime-settings)
# {
#   "debug": false,           // log decisions to the console
#   "update_delay_ms": 32,    // small debounce to coalesce rapid caret moves
#   "max_scan_lines": 5000    // safety limit when walking upwards
# }
# You can also set a per-view flag: view.settings().set("breadcrumb_panel.debug", True)
#
# ## Implementation notes for the curious
#
# - Language agnostic. Ancestors are discovered by walking upwards and selecting the nearest lines
#   with strictly lesser indentation. Indentation is measured in units normalised by tab_size.
# - Small heuristic avoids noisy “closer only” lines such as "):" or "],". This keeps Python-style
#   multi-line headers readable without needing language grammars.
# - Output panel rather than popups. Predictable, persistent and click-navigable. The panel is
#   marked with a setting so event hooks can distinguish it from normal views.
# - Coalesced updates. A tiny async debounce and a monotonic token ensure only the latest scheduled
#   update writes to the panel.
# - Cheap no-op check. Before computing breadcrumbs, the plugin compares buffer_id, current row,
#   the line’s indent units, and the buffer change_count(). If unchanged, it skips work.
# - Dormant when hidden. Event handlers early-out unless the panel is visible and enabled.
#
# ## Limitations and future ideas
#
# - The approach is indentation-driven. It intentionally avoids full parsing. Per-syntax filters
#   (for decorators, attributes or annotations that precede a header) could be added later.

import sublime
import sublime_plugin
from typing import List, Optional, Tuple

PANEL_NAME = "breadcrumb_panel"   # final panel name is "output.breadcrumb_panel"


class Settings:
    """
    Manage settings for the breadcrumb panel.

    This class provides methods to load and merge default settings with user-defined settings. It also
    allows for view-specific settings to be applied.

    Parameters:
    - `view`: The sublime.View object for which to load view-specific settings (optional).

    Returns:
    - A dictionary containing the merged settings.
    """

    @staticmethod
    def load(view: Optional[sublime.View]) -> dict:
        """
        Load and merge default settings with user settings.

        This method loads the default settings for the breadcrumb panel, then merges in any user-defined
        settings from the "Breadcrumb Panel.sublime-settings" file. If a `sublime.View` object is provided,
        it also checks for any view-specific settings.

        Returns:
        - A dictionary containing the merged settings.
        """

        default = {
            "debug": False,
            "max_scan_lines": 5000,
            "update_delay_ms": 32,   # small debounce to coalesce bursts
        }

        s = sublime.load_settings("Breadcrumb Panel.sublime-settings")
        default.update({
            "debug": s.get("debug", default["debug"]),
            "max_scan_lines": s.get("max_scan_lines", default["max_scan_lines"]),
            "update_delay_ms": s.get("update_delay_ms", default["update_delay_ms"]),
        })

        if view:
            v = view.settings()
            if v.has("breadcrumb_panel.debug"):
                default["debug"] = bool(v.get("breadcrumb_panel.debug"))
        return default


def _dbg(enabled: bool, *args) -> None:
    """
    Enable or disable debug logging.

    Parameters:
    - `enabled`: Set debugging state to enabled (`True`) or disabled (`False`).
    - `*args`: Additional log messages to print when debugging is enabled.
    """

    if enabled:
        print("[breadcrumb-panel]", *args)


def _leading_indent_units(view: sublime.View, line_region: sublime.Region) -> int:
    """
    Calculate the number of leading indentation units for a given line.

    This function determines the number of leading spaces or tabs in a line, taking into account
    the tab size setting. It returns the total number of units, rounded down to the nearest whole
    number.

    Parameters:
    - `view`: The Sublime Text view object.
    - `line_region`: The region of the line for which to calculate the indentation.

    Returns:
    - The number of leading indentation units.
    """

    tab_size = int(view.settings().get("tab_size") or 4)
    text = view.substr(line_region)
    i = 0
    cols = 0

    while i < len(text):
        ch = text[i]
        if ch == " ":
            cols += 1
        elif ch == "\t":
            cols += tab_size - (cols % tab_size)
        else:
            break
        i += 1
    return cols // tab_size


def _is_blank(text: str) -> bool:
    """
    Check if a string is blank.

    Parameters:
    - `text`: The input string to check.

    Returns:
    - `True` if the string is blank (i.e., contains only whitespace), `False` otherwise.
    """

    return text.strip() == ""


def _is_only_closer(line_text: str) -> bool:
    """
    Check if a line of text contains only closing brackets or characters.

    This function takes a string representing a line of text and returns `True` if it contains
    only closing brackets or characters (i.e., `)`, `]`, `}`, or `:`), and `False` otherwise.

    Parameters:
    - `line_text`: The input line of text to be checked.

    Returns:
    - `True` if the line contains only closing brackets or characters, `False` otherwise.
    """

    s = line_text.strip()
    if not s:
        return False

    for ch in s:
        if ch not in ")]},:":
            return False
    return True


def _find_breadcrumb_lines(view: sublime.View, row: int, debug: bool, max_scan: int) -> List[Tuple[int, str, int]]:
    """
    Find breadcrumb lines in the current view.

    This function scans up from the current caret position to find lines with a similar indentation
    level. The search is bounded by the maximum number of lines to scan (`max_scan`) and stops when
    it reaches a line with zero indentation units or when it has scanned the maximum number of lines.

    Parameters:
    - `view`: The Sublime Text view to search in.
    - `row`: The current caret row (1-indexed).
    - `debug`: A flag indicating whether to enable debug logging.
    - `max_scan`: The maximum number of lines to scan up from the current caret position.

    Returns:
    - A list of tuples containing the breadcrumb line number (1-indexed), the line text, and the
    indentation units. The list is ordered from top to bottom in the view.
    """

    # Collect all line regions and bail out on empty buffers
    regions = view.lines(sublime.Region(0, view.size()))
    if not regions:
        return []

    # Clamp target row and fetch the caret line region
    row = max(0, min(row, len(regions) - 1))
    current_line = regions[row]

    # Measure current line’s indent (units) and trace it
    curr_units = _leading_indent_units(view, current_line)
    _dbg(debug, "caret_row=", row + 1, "curr_indent_units=", curr_units)

    # If at top level there are no ancestors to show
    if curr_units == 0:
        return []

    # Prepare output and set the initial ancestor indent threshold
    out: List[Tuple[int, str, int]] = []
    target_units = curr_units

    # Walk upwards collecting lines with strictly lesser indent
    i = row - 1
    scanned = 0
    while i >= 0 and target_units > 0 and scanned < max_scan:
        r = regions[i]
        text = view.substr(r)
        units = _leading_indent_units(view, r)
        scanned += 1

        # Ignore blank lines entirely
        if _is_blank(text):
            i -= 1
            continue

        _dbg(debug, "scan_up row=", i + 1, "units=", units, "target=", target_units, "text=", text.rstrip())

        # Accept true ancestors (lesser indent) but skip pure “closer” lines
        if units < target_units and not _is_only_closer(text):
            out.append((view.rowcol(r.begin())[0] + 1, text.rstrip(), units))
            target_units = units
            if target_units == 0:
                break

        i -= 1

    # Present from outermost to nearest and trace the result
    out.reverse()
    _dbg(debug, "crumbs=", [(ln, units) for (ln, _t, units) in out])
    return out


def _current_row_and_units(view: sublime.View) -> Tuple[int, int]:
    """
    Return the current row and units of indentation for the current selection.

    If there is no selection, return (-1, 0).

    Parameters:
    - `view`: The Sublime Text view object.

    Returns:
    - A tuple containing the current row (`int`) and units of indentation (`int`).
    """

    sel = view.sel()
    if not sel:
        return (-1, 0)

    caret = sel[0].begin()
    row, _ = view.rowcol(caret)
    line_region = view.line(caret)
    return (row, _leading_indent_units(view, line_region))


class BreadcrumbPanelState:
    """
    Initialise the plugin instance.

    This class represents the internal state of the BreadcrumbPanel plugin. It abstracts over the
    plugin's initialisation, including setup of debugging flags, panel update schedules, and a
    monotonically increasing token for scheduled updates.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.updating_panel = False
        self.last_key = None
        self.seq = 0                 # monotonically increasing token for scheduled updates
        self.last_scheduled = 0      # id of the most recently scheduled update

        # remember the last *context* that produced the panel
        # keyed by buffer_id -> (row, indent_units, change_count)
        self.context_by_buffer = {}


STATE = BreadcrumbPanelState()


def _panel_view(window: sublime.Window) -> sublime.View:
    """
    Create a panel view for output.

    This function creates a new output panel in the specified window, configuring its settings to
    hide the gutter, line numbers, and enable word wrapping. The panel is also made read-only.

    Parameters:
    - `window`: The Sublime Text window in which to create the panel.

    Returns:
    - A Sublime Text view object representing the created panel.
    """

    panel = window.create_output_panel(PANEL_NAME)
    s = panel.settings()
    s.set("gutter", False)
    s.set("line_numbers", False)
    s.set("scroll_past_end", False)
    s.set("word_wrap", True)
    s.set("breadcrumb_panel", True)  # mark for our listeners
    panel.set_read_only(True)
    return panel


def _set_panel_text(panel: sublime.View, text: str) -> None:
    """
    Clear the current panel text and replace it with the provided string.

    This method temporarily disables read-only mode, selects all text, deletes it, and then
    appends the new text. If an error occurs during this process, the panel will still be restored
    to its original state.

    Parameters:
    - `panel`: The Sublime Text view to update.
    - `text`: The new text to append to the panel.
    """

    STATE.updating_panel = True
    try:
        panel.set_read_only(False)
        panel.run_command("select_all")
        panel.run_command("right_delete")
        panel.run_command("append", {"characters": text, "force": True, "scroll_to_end": False})
    finally:
        panel.set_read_only(True)
        STATE.updating_panel = False


def _format_breadcrumbs(view: sublime.View, debug: bool) -> str:
    """
    Format breadcrumb information for the current view.

    This function generates a string representation of the breadcrumb lines for the current view
    context. The breadcrumb lines are determined by the `_find_breadcrumb_lines` function, which
    searches for indentation patterns in the code. The resulting lines are formatted with
    indentation and text.

    Parameters:
    - `view`: The Sublime Text view object.
    - `debug`: A boolean flag indicating whether to include debug information.

    Returns:
    - A string containing the breadcrumb lines, or an error message if no breadcrumbs were found.
    """

    sels = view.sel()
    if len(sels) != 1:
        return "Multiple contexts\n"

    caret = sels[0].begin()
    row, _ = view.rowcol(caret)

    s = Settings.load(view)
    crumbs = _find_breadcrumb_lines(view, row, debug=s["debug"], max_scan=s["max_scan_lines"])
    if not crumbs:
        return "No indent\n"

    lines = [f"{ln:>6}:  {text}" for (ln, text, _units) in crumbs]
    return "\n".join(lines) + "\n"


def _source_view_for(window: sublime.Window) -> Optional[sublime.View]:
    """
    Prefer the active file in the active group; fall back to first non-widget view.
    Avoids accidentally reading the panel as the source.

    Parameters:
    - `window`: The Sublime Text window for which to find a suitable view.

    Returns:
    - The preferred view, or `None` if no suitable view is found.
    """

    v = window.active_view_in_group(window.active_group())
    if v and not v.settings().get("is_widget"):
        return v

    for cand in window.views():
        if not cand.settings().get("is_widget"):
            return cand
    return None


def _schedule_update(window: sublime.Window, hint_view: Optional[sublime.View]) -> None:
    """
    Update the breadcrumb panel with the current view's context.

    This function is called when a view's context changes. It updates the breadcrumb panel with the
    new context, including the current row and units, and sets the status of the view to indicate
    whether breadcrumbs are active. If the view's context has not changed since the last update, this
    function still updates the context cache to ensure the cheap check is accurate.

    Parameters:
    - `window`: The Sublime Text window object.
    """

    # Only proceed when the feature is enabled and our panel is actually visible
    if not STATE.enabled:
        return
    if not window or window.active_panel() != "output." + PANEL_NAME:
        return

    # Find the current source view; if none, there is nothing to update
    src_view = _source_view_for(window)
    if not src_view:
        return

    # Snapshot the cheap context (buffer id, caret row, indent units, change count)
    buf_id = src_view.buffer_id()
    row, units = _current_row_and_units(src_view)
    cc = src_view.change_count()

    # If nothing relevant changed, skip scheduling altogether
    prev = STATE.context_by_buffer.get(buf_id)
    if prev and prev == (row, units, cc):
        # Same line, same indent, and file unchanged — context unchanged
        return

    # Read debounce settings and prepare a new coalescing token
    s = Settings.load(hint_view)
    delay = int(s["update_delay_ms"])

    # Bump the sequence and mark this schedule as the latest
    STATE.seq += 1
    token = STATE.seq
    STATE.last_scheduled = token

    def _run():
        """
        Update the breadcrumb panel with the current view's context.

        This function is called when a view's context changes. It updates the breadcrumb panel with
        the new context, including the current row and units, and sets the status of the view to
        indicate whether breadcrumbs are active. If the view's context has not changed since the last
        update, this function still updates the context cache to ensure the cheap check is accurate.

        Parameters:
        - `window`: The Sublime Text window object.
        """

        # Drop stale scheduled runs that lost the coalescing race
        if token != STATE.last_scheduled:
            return

        # Resolve the current source view; if none, there is nothing to render
        src_view = _source_view_for(window)
        if not src_view:
            return

        # Build a cache key based on the latest selection state
        sels = list(src_view.sel())
        sels_hash = tuple((r.a, r.b) for r in sels)
        row = src_view.rowcol(sels[0].begin())[0] if sels else -1

        # Skip if identical to the last render; still refresh the cheap context cache
        key = (src_view.buffer_id(), row, sels_hash)
        if key == STATE.last_key:
            STATE.context_by_buffer[src_view.buffer_id()] = (row, _current_row_and_units(src_view)[1], src_view.change_count())
            return
        STATE.last_key = key

        # Compute and paint the breadcrumbs into the output panel
        panel = _panel_view(window)
        text = _format_breadcrumbs(src_view, debug=Settings.load(src_view)["debug"])
        _set_panel_text(panel, text)
        src_view.set_status("breadcrumb_panel", f"Breadcrumbs: {'active' if STATE.enabled else 'off'} — {text.strip()[:60]}")

        # Record the context that produced this panel content for future cheap checks
        cur_row, cur_units = _current_row_and_units(src_view)
        STATE.context_by_buffer[src_view.buffer_id()] = (cur_row, cur_units, src_view.change_count())

        _dbg(Settings.load(src_view)["debug"], "update_panel done; text=", repr(text))

    # Run async after a tiny delay to let caret/layout settle
    sublime.set_timeout_async(_run, delay)


class ToggleBreadcrumbPanelCommand(sublime_plugin.WindowCommand):
    """
    Toggle the visibility of the output panel.

    This call toggles the visibility of the output panel, hiding it if it is currently active and
    showing it otherwise. If the panel is hidden, plugin functionality is suspended until it is
    shown again.

    Parameters:
    - `self`: The current plugin instance.

    Notes:
    This can raise if there are any issues with hiding or showing the panel.
    """

    def run(self) -> None:
        w = self.window
        if w.active_panel() == "output." + PANEL_NAME:
            STATE.enabled = False
            STATE.last_key = None

            for v in w.views():
                v.erase_status("breadcrumb_panel")

            w.run_command("hide_panel", {"panel": "output." + PANEL_NAME})
            _dbg(True, "panel hidden, plugin dormant")
        else:
            STATE.enabled = True
            _panel_view(w)
            w.run_command("show_panel", {"panel": "output." + PANEL_NAME})
            _schedule_update(w, w.active_view())


class BreadcrumbPanelListener(sublime_plugin.EventListener):
    """
    Breadcrumb Panel Listener

    This class listens for various events in Sublime Text and updates the breadcrumb panel accordingly.

    It checks if the current view should be ignored, and if not, schedules an update of its associated
    window on selection modification, activation, and modification. It also handles asynchronous
    selection modification events and text commands.

    The class provides methods to determine whether a view should be ignored, navigate to a specific
    line in the source view from a given panel, and handle text commands.

    Parameters:
    - `view`: The Sublime Text view associated with this listener.

    Notes:
    This class is an EventListener and must be used in conjunction with the Sublime Text API.
    """

    def _should_ignore(self, view: sublime.View) -> bool:
        """
        Determine whether the current view should be ignored.

        This method checks if the view is a widget or if widget rendering is disabled in the state.

        Parameters:
        - `view`: The Sublime Text view to check.

        Returns:
        - `True` if the view should be ignored, `False` otherwise.
        """

        return bool(view.settings().get("is_widget") or not STATE.enabled)

    def on_activated(self, view: sublime.View) -> None:
        """
        Schedule an update of the view's window.

        This call checks if the view should be ignored, and if not, schedules an update of its associated
        window.
        """

        if self._should_ignore(view):
            return

        w = view.window()
        if w:
            _schedule_update(w, view)

    def on_selection_modified(self, view: sublime.View) -> None:
        """
        Update the view window if necessary.

        This call checks if the current selection should be ignored, and if not, schedules an update of the
        view window.
        """

        if self._should_ignore(view):
            return

        w = view.window()
        if w:
            _schedule_update(w, view)

    def on_selection_modified_async(self, view: sublime.View) -> None:
        """
        Handle asynchronous selection modification events.

        If the view should be ignored (as determined by `_should_ignore`), this method does nothing.
        Otherwise, it updates the window associated with the view, scheduling an update if necessary.
        """

        if self._should_ignore(view):
            return

        w = view.window()
        if w:
            _schedule_update(w, view)

    def on_modified(self, view: sublime.View) -> None:
        """
        Update the view window if necessary after a modification.

        This call checks if the current view should be ignored, and if not, updates the associated window.
        If the view is part of a window, the window is scheduled for an update.
        """

        if self._should_ignore(view):
            return

        w = view.window()
        if w:
            _schedule_update(w, view)

    def on_text_command(self, view: sublime.View, command_name: str, args: dict):
        """
        Handle a text command in the view.

        This call is invoked when a text command is executed in the view. It checks if the breadcrumb panel
        is enabled and not currently being updated, and if the command name matches "drag_select". If these
        conditions are met, it schedules the `_navigate_from_panel` method to be called immediately.
        """

        if not view.settings().get("breadcrumb_panel") or STATE.updating_panel:
            return None
        if command_name != "drag_select":
            return None

        sublime.set_timeout(lambda: self._navigate_from_panel(view), 0)
        return None

    def _navigate_from_panel(self, panel: sublime.View) -> None:
        """
        Navigate to a specific line in the source view from the given panel.

        This method checks if the panel is currently being updated, and if so, it returns immediately.
        Otherwise, it extracts the window, source view, selection, and line text from the panel.
        It then attempts to parse the line text as a line number prefix, and if successful, it navigates
        the source view to that line.

        Parameters:
        - `panel`: The sublime View instance for which to navigate.
        """

        # Ignore clicks while we are programmatically updating the panel
        if STATE.updating_panel:
            return

        # Resolve window and bail if none
        w = panel.window()
        if not w:
            return

        # Find the current source view to navigate within
        src = _source_view_for(w)
        if not src:
            return

        # Read the caret line in the panel
        sel = panel.sel()
        if not sel:
            return

        # Get the full text of the clicked panel line
        row, _ = panel.rowcol(sel[0].begin())
        line_region = panel.line(panel.text_point(row, 0))

        # Expect the “NNN:  …” format; otherwise ignore
        text = panel.substr(line_region).strip()
        if not text or ":" not in text:
            return

        # Extract the numeric line prefix
        prefix = text.split(":", 1)[0]

        # Parse the target line number; ignore malformed input
        try:
            line_no = int(prefix)
        except ValueError:
            return

        # Move the caret to that line in the source and reveal it
        pt = src.text_point(max(0, line_no - 1), 0)
        src.sel().clear()
        src.sel().add(sublime.Region(pt))
        src.show_at_center(pt)


class ToggleBreadcrumbPanelDebugCommand(sublime_plugin.WindowCommand):
    """
    Toggle the debug state of the Breadcrumb Panel.

    This class provides a command to toggle the debug state of the active view and displays a status
    message indicating the new state.

    Parameters:
    - `self`: The instance of the plugin class.

    Notes:
    - This can raise if there is any issue with updating the view settings.
    - The `_schedule_update` function is called to schedule an update of the Breadcrumb Panel.
    """

    def run(self) -> None:
        v = self.window.active_view()
        if not v:
            return

        current = bool(v.settings().get("breadcrumb_panel.debug", False))
        v.settings().set("breadcrumb_panel.debug", not current)
        sublime.status_message("Breadcrumb Panel debug: {}".format("ON" if not current else "OFF"))
        _schedule_update(self.window, v)

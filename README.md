==============================================================================
 DYNAMIC ISLAND for KDE Plasma (Wayland)  -  GTK4 edition
 An iPhone-style Dynamic Island at the top-center of your screen.
 Python + GTK4 + gtk4-layer-shell. Runs as a NATIVE Wayland layer-shell surface.
==============================================================================

WHY THIS EDITION
  The earlier PySide6 version ran through XWayland, and KDE Wayland does not
  deliver drag & drop to such windows. This edition is a real Wayland surface, so
  you can drag files onto the island AND drag them back out into Dolphin.

CONTENTS
  1. Files               8. File shelf
  2. Requirements        9. Themes and custom colors
  3. Install/update/remove 10. CTF events, reminders, Pomodoro
  4. What it does        11. Config file reference
  5. Mouse and buttons   12. Troubleshooting
  6. Terminal commands   13. What changed from the PySide6 edition
  7. Visibility modes    14. Known limitations / what was tested


------------------------------------------------------------------------------
1. FILES
------------------------------------------------------------------------------
  island.py    the program (one file)
  install.sh   installer
  README.txt   this file


------------------------------------------------------------------------------
2. REQUIREMENTS
------------------------------------------------------------------------------
  - KDE Plasma in a WAYLAND session (log in with "Plasma (Wayland)").
  - Everything else is installed by install.sh:
      python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, playerctl, pulseaudio-utils,
      network-manager, iproute2, wl-clipboard, dbus-bin, icon/svg support,
      and gtk4-layer-shell (from apt if available, otherwise built from source
      into ~/.local/share/dynamic-island/gls - needs git, meson, ninja, gcc).
  - No PySide6 and no virtual environment are needed any more.


------------------------------------------------------------------------------
3. INSTALL / UPDATE / REMOVE
------------------------------------------------------------------------------
INSTALL (also replaces the old PySide6 version)
      cd ~/Downloads            (the folder with island.py and install.sh)
      bash install.sh

  It stops the old island, installs packages (asks for sudo once), installs
  gtk4-layer-shell, copies the program to ~/.local/share/dynamic-island/, creates
  the "island" command (~/.local/bin/island), adds "Add to Island Shelf" to the
  Dolphin right-click menu, and starts a systemd user service (starts at login,
  restarts on crash). Your old config (~/.config/dynamic-island/config.json) is
  kept and still works.

UPDATE (new island.py later)
      cp ~/Downloads/island.py ~/.local/share/dynamic-island/island.py
      systemctl --user restart dynamic-island

SERVICE CONTROL
      systemctl --user status|restart|stop dynamic-island
      journalctl --user -u dynamic-island -f          (live log)

REMOVE
      systemctl --user disable --now dynamic-island
      rm -rf ~/.local/share/dynamic-island ~/.local/bin/island
      rm -f  ~/.config/systemd/user/dynamic-island.service
      rm -f  ~/.local/share/kio/servicemenus/island-shelf.desktop
      rm -rf ~/.config/dynamic-island                  (your settings)


------------------------------------------------------------------------------
4. WHAT IT DOES
------------------------------------------------------------------------------
  Music          Album art, title/artist, progress (click to seek), prev /
                 play-pause / next (MPRIS via playerctl). Equalizer and album
                 color accent in the collapsed pill; track-change pop-up.
  Notifications  Pops down for any app's notification (read from D-Bus).
  Volume         Overlay with bar, percentage and mute (PulseAudio/PipeWire).
  Brightness     Overlay (laptops with a backlight).
  Battery        Plug / unplug pop-ups, low-battery warning (20% and 10%).
  Caps/Num Lock  On/off pop-up (reads the keyboard LEDs from /sys).
  VPN            "VPN connected - 10.10.14.5" for tun0/wg0/tap0/ppp0/tailscale0,
                 red message when it drops, green "VPN" badge while idle.
  Wi-Fi / LAN    Pop-up when the network connects or disconnects.
  Timer, Pomodoro (25/5, long break after 4), Clock, System page (CPU graph,
  RAM, network speed, temperature), CTF countdown + reminders, app Launcher,
  File shelf, Themes, Modes, Settings page, tray icon.


------------------------------------------------------------------------------
5. MOUSE AND BUTTONS
------------------------------------------------------------------------------
  Left click        expand / collapse (click a notification to dismiss it)
  Middle click      play / pause
  Right click       open the Settings page (replaces the old right-click menu)
  Mouse wheel       switch pages while expanded
  Dots (bottom)     jump to a page
  Bottom-right chip current MODE; click = next mode
  Bottom-left chip  current THEME; click = next theme
  Tray icon         left click = next mode, middle click = next theme,
                    right click = open Settings on the island
                    (the tray is how you bring the island back in Popup/Closed)

  Pages: Home | Shelf | System | Focus | Launch | Settings
  The expanded view closes itself ~2.5 s after the mouse leaves it (6 s when it
  was opened by a command or the tray and the mouse is elsewhere).
  Settings page: mode buttons, theme buttons, quick timers 5/15/25 min,
  Pomodoro start/stop, Clear shelf, Quit island.


------------------------------------------------------------------------------
6. TERMINAL COMMANDS
------------------------------------------------------------------------------
  island timer 5m              (90 = seconds, 5m, 1h, 25m ...)
  island cancel                cancel timer / pomodoro
  island toast "Scan done"
  island toggle                expand / collapse
  island page NAME             home | shelf | system | focus | launch | settings
  island pomodoro [stop|skip]
  island ctf "NAME" "WHEN"     island remind "NAME" "WHEN" [daily|weekly]
  island forget "NAME"
  island mode auto|standby|popup|closed|cycle
  island theme NAME|next       island style KEY VALUE | style reset
  island shelf                 open the shelf page
  island shelf add FILE...     island shelf paste | copy | clear
  island reload                re-read config.json
  island quit

  WHEN:  "2026-11-20 19:00"  |  "19:00" (next time it happens)  |  "+30m"
  Tip:   nmap -sV target -oN scan.txt; island toast "Scan finished"
  Tip:   bind keys in System Settings > Shortcuts > Add Command, e.g.
         "island mode cycle" or "island shelf paste".


------------------------------------------------------------------------------
7. VISIBILITY MODES
------------------------------------------------------------------------------
  Standby    always visible as an idle pill (default)
  Popup      hidden until something happens, then pops out and hides again
  Closed     fully hidden; nothing shows, including alerts (do-not-disturb)
  Automatic  Standby while music is PLAYING / a timer runs / the island is
             expanded; otherwise Popup; Closed during "quiet_hours"
             (config: "quiet_hours": ["23:00", "07:00"])

  Change with the bottom-right chip, the Settings page, the tray icon or
  "island mode ...". Saved across restarts.
  Explicit actions (the tray, "island toggle", "island page ...") show the
  island for ~8 s even in Closed mode, so you can never lock yourself out.
  Drag & drop only works while the island is visible (Standby, or Automatic
  while something is playing / running).


------------------------------------------------------------------------------
8. FILE SHELF
------------------------------------------------------------------------------
  Holds up to 5 files/folders, in memory only: gone when the island quits. It
  stores references, not copies - if you move or delete the original, it
  disappears from the shelf.

  ADD     - drag files/folders from Dolphin onto the island (it opens the shelf)
          - right-click files in Dolphin > "Add to Island Shelf"
          - copy files in Dolphin (Ctrl+C), then "Paste" on the Shelf page or
            "island shelf paste"
          - island shelf add FILE...
  USE     - drag a row out into Dolphin or any app (Shift+drag = all items)
          - click a row (or "Copy all") and press Ctrl+V in a file manager
  REMOVE  - the x on a row, or "Clear"
  Auto-expiry (optional): "shelf_expire_minutes": 120   (0 = never)
  A small count badge shows in the idle pill while files wait on the shelf.


------------------------------------------------------------------------------
9. THEMES AND CUSTOM COLORS
------------------------------------------------------------------------------
  black, glass (dark ~60% see-through), clear (very transparent), light,
  midnight, hacker (green monospace), sunset, album (tinted with the cover).
  Change: island theme glass | the bottom-left chip | Settings page | tray.

  Tweak the current theme (switching theme clears the tweaks):
    island style opacity 0.35          island style bg "#102030"
    island style bg2 "#401030"         (gradient)
    island style text "#ffd479"        island style accent "#ff5577"
    island style border_opacity 0.2    island style corner 20
    island style font "Fira Code"      island style tint 0.4
    island style art_accent false      island style reset

  Your own theme in config.json, then "island theme mine":
    "themes": { "mine": { "label": "Mine", "bg": "#202020", "opacity": 0.5,
                          "text": "#ffd479", "accent": "#ff5577" } }

  Very transparent themes can be hard to read on bright wallpapers; "glass" at
  opacity 0.6-0.7 is a good balance. Background blur is not available.


------------------------------------------------------------------------------
10. CTF EVENTS, REMINDERS, POMODORO
------------------------------------------------------------------------------
  island ctf "HTB Cyber Apocalypse" "2026-11-20 19:00"
  island remind "Network Security class" "08:00" daily
  Alerts: CTF 1 day / 1 hour / 10 min before and at the start; reminders
  10 min before and at the start. The Focus page shows the countdown to the next
  CTF and the next reminder. Alerts that were due while the island was off are
  not shown later (only those due within ~2 minutes). Closed mode skips alerts.


------------------------------------------------------------------------------
11. CONFIG FILE REFERENCE   (~/.config/dynamic-island/config.json)
------------------------------------------------------------------------------
  "mode"                  auto | standby | popup | closed
  "quiet_hours"           [] or ["23:00", "07:00"]
  "theme" / "themes" / "style"   see section 9
  "events"                CTF events / reminders (edited by the commands)
  "launchers"             up to 8 buttons on the Launch page:
                          { "name": "Burp", "cmd": "burpsuite", "icon": "burpsuite" }
  "shelf_expire_minutes"  0 = never
  After editing run:  island reload
  (The old keys "x11_bypass_wm" and "input_mask" are no longer used.)


------------------------------------------------------------------------------
12. TROUBLESHOOTING
------------------------------------------------------------------------------
  Nothing appears
    systemctl --user status dynamic-island
    journalctl --user -u dynamic-island -n 40
    Check the mode chip / tray: Popup and Closed hide the island.
    echo $XDG_SESSION_TYPE   must print "wayland".

  It shows up as a normal window, not at the top
    gtk4-layer-shell is missing or KDE rejected it. Run by hand to see:
      ~/.local/bin/island            (prints "Island is already running" if up)
      systemctl --user stop dynamic-island
      GDK_BACKEND=wayland python3 ~/.local/share/dynamic-island/island.py
    A line "layer-shell not available" confirms it. Re-run install.sh.

  Drag and drop does nothing
    Make sure the island is visible (Standby). Debug:
      systemctl --user stop dynamic-island
      ISLAND_DEBUG=1 python3 ~/.local/share/dynamic-island/island.py
    Drag a file over the pill: "[island] drag enter" must print. If it does but
    nothing appears, report it; if it does not, KDE is not delivering the drag.
    Meanwhile the Dolphin right-click entry and Paste always work.
    (Run "systemctl --user start dynamic-island" again afterwards.)

  "Copy" then Ctrl+V pastes nothing
    Needs wl-clipboard:  which wl-copy wl-paste   (sudo apt install wl-clipboard)

  No tray icon
    Plasma's system tray must be present. The island still works without it.

  No notifications:  which dbus-monitor   (sudo apt install dbus-bin)
  No volume/music:   which pactl playerctl
  Caps Lock pop-up never shows: needs LEDs under /sys/class/leds (many VMs lack them)
  Wi-Fi name missing: needs nmcli (network-manager)


------------------------------------------------------------------------------
13. WHAT CHANGED FROM THE PySide6 EDITION
------------------------------------------------------------------------------
  + Native Wayland layer-shell: drag & drop works in and out.
  + No PySide6 / venv (uses the system python3-gi): smaller install.
  + New Settings page (modes, themes, timers, quit) replaces the right-click menu.
  + Tray icon is a minimal StatusNotifierItem (click actions, no menu).
  + Explicit commands/tray "peek" even in Closed mode (8 s).
  + Clipboard goes through wl-copy / wl-paste (works without keyboard focus).
  - X11 sessions are not supported by this edition (it would open as a plain
    window). Keep the PySide6 edition for X11.


------------------------------------------------------------------------------
14. KNOWN LIMITATIONS / WHAT WAS TESTED
------------------------------------------------------------------------------
  Tested in a headless Wayland compositor (sway, which also implements
  layer-shell) with real pointer events: layout of every page, themes, clicks,
  dots/chips, drag files IN and OUT (with a GTK app standing in for Dolphin),
  clipboard copy/paste, tray over D-Bus, notifications, all four modes, and the
  installer including the build-from-source path.
  NOT tested on a real KDE desktop (KWin): placement above the Plasma panel,
  dragging from the real Dolphin, the tray icon in Plasma's system tray, and
  clipboard behaviour with Klipper. Please report anything that differs.
  - Playing media: playerctl follows the first player it finds.
  - Primary monitor only (the compositor decides which output).
  - It may appear over fullscreen apps (overlay layer).
  - The shelf is memory-only by design.

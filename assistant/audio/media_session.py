"""Jarvis's music in the Windows media controls: the mini player in the volume / notification flyout, the lock screen
and the keyboard's media keys (play/pause, next, previous) control it like any other player.

The music is played by our own player (assistant/audio/music.py), so the system controls are driven by hand:
a WinRT MediaPlayer that plays nothing lends its SystemMediaTransportControls, its own command handling is off.
Needs the pywinrt packages (requirements.txt); without them music works as before, only without the mini player.
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from assistant.audio.music import MusicPlayer

log = logging.getLogger("music")


class MediaSession:
    def __init__(self, player: "MusicPlayer") -> None:
        self.player = player
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._smtc = None
        self._shown: tuple = ()
        self._thread = threading.Thread(target=self._run, name="media-session", daemon=True)
        self._thread.start()
        player.listeners.append(self.changed)

    def changed(self) -> None:
        """The player changed state or track (any thread, also the audio callback): updated in the background."""
        self._jobs.put(None)

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        try:
            self._smtc = self._create()
        except Exception as exc:  # pywinrt missing or no media controls on this Windows
            log.info("Мини-плеер Windows недоступен (%s): музыкой управляют голосом", exc)
            return
        while True:
            self._jobs.get()
            while not self._jobs.empty():  # several changes at once: show the last state
                self._jobs.get()
            try:
                self._update()
            except Exception:
                log.exception("Мини-плеер Windows: не обновился")

    def _create(self):
        from winrt.windows.media import SystemMediaTransportControlsButton as Button
        from winrt.windows.media.playback import MediaPlayer

        self._mp = MediaPlayer()                 # kept alive: the controls belong to it
        self._mp.command_manager.is_enabled = False
        smtc = self._mp.system_media_transport_controls
        smtc.is_play_enabled = smtc.is_pause_enabled = smtc.is_stop_enabled = True
        smtc.is_next_enabled = smtc.is_previous_enabled = True
        smtc.is_enabled = False
        actions = {Button.PLAY: self.player.resume, Button.PAUSE: self.player.pause, Button.STOP: self.player.stop,
                   Button.NEXT: self.player.next, Button.PREVIOUS: self.player.prev}

        def pressed(_sender, args) -> None:
            action = actions.get(args.button)
            if action is not None:
                log.info("Мини-плеер Windows: %s", args.button.name.lower())
                action()
        smtc.add_button_pressed(pressed)
        return smtc

    def _update(self) -> None:
        from winrt.windows.media import MediaPlaybackStatus, MediaPlaybackType

        state, track = self.player.state, self.player.current
        if (state, track) == self._shown:
            return
        self._shown = (state, track)
        smtc = self._smtc
        if state == "stopped" or track is None:
            smtc.playback_status = MediaPlaybackStatus.STOPPED
            smtc.is_enabled = False  # the mini player disappears
            return
        title, artist, album = describe(track)
        updater = smtc.display_updater
        updater.type = MediaPlaybackType.MUSIC
        updater.music_properties.title = title
        updater.music_properties.artist = artist
        updater.music_properties.album_title = album
        updater.update()
        smtc.is_enabled = True
        smtc.playback_status = MediaPlaybackStatus.PLAYING if state == "playing" else MediaPlaybackStatus.PAUSED


def describe(track: Path) -> tuple[str, str, str]:
    """(title, artist, album) from the file's tags; the file name and the folder (playlist) when there are none."""
    title, artist, album = track.stem, "", track.parent.name
    try:
        import av

        with av.open(str(track)) as container:
            tags = {k.lower(): str(v) for k, v in container.metadata.items()}
        title = tags.get("title") or title
        artist = tags.get("artist") or tags.get("album_artist") or ""
        album = tags.get("album") or album
    except Exception:  # no PyAV or no tags: the file name is good enough
        pass
    if not artist and " - " in title:  # "Artist - Song.mp3"
        artist, title = (part.strip() for part in title.split(" - ", 1))
    return title, artist or "Джарвис", album

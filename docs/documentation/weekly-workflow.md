# Weekly workflow

What you do in the app on a normal week. Nothing changes in Ableton until you press **Apply** on a list of changes the assistant shows you.

## 1. Bring in a song

Press **Import song files from a folder** (under the message box, or on the welcome screen). Pick a folder, add a note under "Anything I should know?" if you want ("this one is in D"), and press **Import song files**. Pick either one song's folder or a folder holding several songs; it looks up to three levels down and reads up to 200 files. Holy Sound listens to each file ("Listening to your files…"), then the assistant suggests what to do.

Every song lands on the same set of **part tracks**, in the same order, every week: Click, Guide, SMPTE, Loops, Drums, Perc, Synth Bass, Bass, Acoustic, Electric, Piano, Organ, Keys, Strings, Horns, Synths, FX, Lead Vocal, Choir, BGVs, Crowd. A stem with a name that fits none of those keeps its own name unless the assistant can tell what it is (a singer's name becomes a vocal part, for example). More in [part-tracks-and-imports.md](part-tracks-and-imports.md).

- Where one part has several stems (Kick In, Kick Out, Snare Top...), they are mixed into one file for that song. Left and right stems keep their sides. The original stems are not touched; the mix is saved under `~/Music/Holy Sound/Parts`.
- Part tracks are created when they are missing, with **No Input**, a fader low enough that the whole song does not clip, and their folder's colour. A new SMPTE track starts muted.
- Levels are evened out inside each song, so one fader setting suits every song.
- The assistant tells you the key it heard ("Sounds like it's in E"). It is a guess from the pitched stems (keys, pads, guitars, bass); when it is not clear, it names the runner-up and asks you.

### Stems that came with a vendor's Ableton set

If the folder (or its parent) holds exactly one `.als` file from a vendor such as Washed or MultiTracks, and it matches the stems you imported, Holy Sound reads it for the song's tempo, its sections and which stem is which. It only reads the file. It never changes it, and it does not open it. Live 8 sets work. The song comes into your own set as a new song at that tempo. The assistant knows the sections but cannot jump to one yet.

## 2. Apply

The list of changes sits just above the message box, headed "3 changes, not applied yet". Each step is in plain words. A step that removes something says so in red ("Removes this track from your set."); a step that makes sound says "You will hear this in the room."

Press **Apply 3 changes** to run them in order, or **Not now** to leave them. While they run, each step shows Working, then Done, Check or Failed, and the heading ends as "3 changes applied" or "2 applied, 1 needs a look". If one step fails, the rest still run, and the failed one says why. There is no undo button here; use Cmd+Z in Ableton.

## 3. Change one channel

On the **Mixer** tab every channel has a balance bar at the top, **Mute** and **Solo**, a fader, and its name at the bottom on its colour. Channels sit in folders (Vocals, Instruments, Click & playback, Other, and Shared effects for reverb and delay); press a folder's name to fold it away.

Tap a channel's name to open it in the panel below the mixer. There you can rename it, change its colour, set its balance, turn its reverb and delay up or down, add or remove effects, move it to another folder, and choose where it plays: the room (main speakers), another output such as the in-ears, or only the shared effects. Levels read as percentages ("68%") and "Off" at the bottom.

On a computer you can also drag a channel's name onto another folder. Ableton can't show folders, so moving a channel also gives it that folder's colour in Ableton; **Copy folder colours to Ableton** does that for every channel at once.

## 4. Mix one song

On the **Mixer** tab, pick a song in **Song mix** at the top. The mixer then shows only the tracks that play in that song, and everything you change is saved to that song: faders, balance, Mute and reverb and delay. "Changes save to Way Maker" beside the picker reminds you. When that song is picked again, or started with **Start** on the Songs tab or in Ableton, its mix comes back. Solo and the Main level are not saved per song.

Tap a channel's name, and the panel below has **In Way Maker** with two more controls for that song only:

- A switch that reads **Playing in this song** or **Left out of this song**. Use it for a part your band plays live, rather than muting the track. A part left out greys out on the mixer and reads "Left out".
- A **Level** for that song only (-24 to +12 dB).

These two are saved in the song itself, so Ableton applies them when the song starts, with or without Holy Sound open.

**Checkpoints** keeps a named copy of a song's mix ("After soundcheck"). Open it, name the mix and press **Save checkpoint**. **Go back to this** puts that mix back, and first saves the mix it replaces as a checkpoint, so you can change your mind.

Choose **Every track** to go back to the full mixer. With no song picked, or with Holy Sound closed, the faders are shared by every song.

## 5. Change a song's key

On the **Songs** tab each song has a −, a key button and a +. The key button reads "Original key" or, say, "Up 2 half steps"; press it to go back to the original key. A song that is not in its original key stays lit so nobody forgets it on Sunday. The song keeps its tempo.

- Click, guide, count and SMPTE keep their key by default. For any other track, tap its name on the Mixer and tick or untick **Changes with the song key** under "Where it plays". Your choice is remembered by track name.
- You can also ask the assistant ("put Way Maker up a whole step"), or run `uv run rig.py song transpose "Way Maker" 2`.
- The key control is hidden if Ableton is running an old RigLink. Restart Ableton.

## 6. Order the songs

The Songs tab is your running order: number, title, BPM, key and **Start**. You can rename a song or change its BPM right there, and add one at the bottom. To move a song or add one at a position, ask the assistant, or use the CLI: `song add NAME --at 3` and `song move SONG 1`. Moving copies the song's clips into the new slot and deletes the old one; it is undoable in Ableton, one step per clip.

## 7. Tidy an old set

A set made before part tracks may have one track per stem (one grew to 93 tracks for three songs). Ask the assistant to tidy it, or run `uv run rig.py track tidy`. It rebuilds every song on the part tracks, keeps how each song sounds (each song's levels and the old faders are mixed in, part faders end at 0 dB, transposed songs stay transposed) and deletes the emptied stem tracks. Stems left out of a song, or on a muted track, are left out. Effects and sends on the old tracks are not carried over, and it says which ones. **Save a copy of the set first.** It takes a minute or two per song.

## With Ableton closed

A list of changes that adds tracks can be saved with **Save as an Ableton file** instead of applied. Only the tracks come along: name, audio or MIDI, hardware input, effects, volume and balance. Everything else (songs, clips, colours, routing to outputs) needs Ableton open, and the list notes what was not in the file. Do not save a file over a set that is open in Ableton.

See also: [troubleshooting.md](troubleshooting.md), [cli-and-live-control.md](cli-and-live-control.md).

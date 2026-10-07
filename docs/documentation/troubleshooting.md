# Troubleshooting

Messages are quoted as the app words them.

## Ableton and RigLink

| What you see | What it means | Fix |
| --- | --- | --- |
| "Ableton isn't connected" at the top, the Mixer and Songs tabs show three steps, or "Can't reach Ableton Live..." | Ableton is closed, or RigLink is not selected. | Open Ableton. **Settings**, then **Link, Tempo & MIDI**, and under Control Surface pick **RigLink**. The app reconnects on its own; no restart. |
| `unknown cmd`, "Live is running an older RigLink. Quit and reopen Live, then try again." | Ableton loaded RigLink before it was updated, and it imports RigLink only once at startup. | Quit and reopen Ableton. Edits to the app or `rig.py` never need this; edits under `ableton_script/RigLink/` always do. Newer commands such as `set_clip_active` (the Playing / Left out switch), `move_scene` and `delete_clip` fail this way on old code. |
| The key control (−, key, +) is missing from a song on the Songs tab | Old RigLink: it does not report each song's key. | Restart Ableton. |
| "Live stopped responding before it finished." | Ableton was busy (loading an effect or importing a big file blocks it for a moment). | Try again. |
| "Demo set, not Ableton" at the top | The app was started with `--fake-live`. Nothing you do touches Ableton. | Start it without `--fake-live`. |
| `uv run rig.py status` fails | Same as the first row. | Same fix. |

RigLink writes `RigLink: listening on 127.0.0.1:9877` to Ableton's `Log.txt` when it starts, and `RigLink: failed to open socket: ...` if the port is taken.

## Sound

- **A mic is silent.** Holy Sound cannot arm a track or set its monitoring yet (the most common cause, and the top gap in [../td_next.md](../td_next.md)). Arm the track and set monitoring to In or Auto by hand in Ableton. Also check the track's input: tap its name on the Mixer, and the panel below says where its sound comes from ("Input 1" or "Playback track"). New playback tracks are created with **No Input**, so a live source must have its input set (ask the assistant, or `rig.py route in`).
- **A playback track picks up a mic.** Tracks Holy Sound creates have No Input. Ableton's own default would be input 1. Tracks made by hand in Ableton keep Ableton's default.
- **Click or guide went to the wrong place.** Click and Guide should go to the in-ear output, not the room. Tap the track's name on the Mixer and, under "Where it plays", set **Plays to** to "Another output (in-ears, etc.)" and pick the output. Or ask the assistant; `rig.py route out Click "Ext. Out" 1` does the same. Hardware output routing in saved Ableton files is not known yet; use Ableton open for that.
- **A part is silent in one song only.** It may be left out of that song. Pick the song in **Song mix**: a part left out greys out and reads "Left out". Tap its name and press **Left out of this song** to bring it back.
- **A song's levels jumped when it started.** Each song keeps its own mix. Picking a song in **Song mix**, or starting it with **Start** or in Ableton, puts its saved faders, balance, mute and reverb and delay back. Change it with the song picked and the change is saved to that song, or open **Checkpoints** and **Go back to this** on an earlier mix.
- **A transposed song sounds right but one part is in the wrong key.** Click, guide, count and SMPTE keep their key on purpose. Tap the track's name and check **Changes with the song key**.
- **Levels look odd on the first play.** The first play after a reset gives bogus meter readings; play again.

## Importing

| Message | Cause |
| --- | --- |
| "I don't have a file called X. Import its folder first." | The assistant used a file that is not in an imported folder. Import the folder with **Import song files from a folder**, or use the imported list. |
| "Those stems have different sample rates, so they can't be mixed into one." | One part's stems differ in sample rate. Import them separately or resample them first. |
| "Couldn't read X to mix it." | The mixer reads WAV and AIFF. Other formats cannot be mixed into a part. |
| "There's no folder at ..." / "There are no audio files in ..." | `rig.py song import` was pointed at the wrong folder. |
| "There's already a song called ..." | `rig.py song import` never overwrites a song. Rename or delete the old one. |
| "There are only N song slots. Add a song first to make room." | A song cannot be moved past the last slot. |
| A clip fails to import | The song already has a clip on that part track. Clips cannot be replaced or deleted by the assistant; delete it in Ableton (Cmd+Z undoes). |
| The vendor set was not read | Needs exactly one `.als` in the folder or its parent, and at least half its stems must be in what you imported. A file Holy Sound cannot read is reported as "isn't an Ableton set Holy Sound can read". |
| The key guess is wrong | It is a guess, and the usual miss is the relative key (E major and C# minor share every note). The assistant names the runner-up; tell it the right one. |

Only the first 200 files in a folder are read. More in [part-tracks-and-imports.md](part-tracks-and-imports.md).

## Tidy and part tracks

- **Tidy left effects behind.** `tidy_into_parts` does not carry effects or sends from the old stem tracks. It lists what was lost in its result.
- **Tidy was interrupted.** It changes the open set step by step and is not transactional. Undo in Ableton (many steps) or reopen your saved copy, which is why it asks you to save one first.
- **A stem got its own track.** Its name fits no part pattern. Ask the assistant to map it to a part. Part patterns live in `app/parts.py`.

## Files

- **Never save an Ableton file over a set that is open in Ableton.** Ableton reads the file once and holds it in memory; the rewrite does nothing and is overwritten on Ableton's next save.
- **A saved file asks Ableton to repair it.** That would be a renderer bug (`check_pointee_pool` runs before writing, but Ableton reconciles silently). Report it with the file. See [file-renderer.md](file-renderer.md) and [ableton-format-notes.md](ableton-format-notes.md).
- **Save as an Ableton file fails with "...needs Ableton Live 12 installed on this computer...".** The renderer copies stock effects from Ableton's own library, so it needs Ableton's install folder.

## The assistant

- "Type a message first.", or a banner at the top of the chat: no key set, or a bad one. Check `.env`. See [assistant-and-actions.md](assistant-and-actions.md#errors) for the full list.
- A reply stops with a generic apology after too many tool steps. Send the message again; a failed turn leaves no trace.
- **The chat is full of old changes.** **Start over** above the chat clears it. Your Ableton set stays as it is.
- **Phone cannot connect.** Start with `--lan` and open the full link it prints, including `?key=...`, on the same Wi-Fi.

# Ableton format notes

The `.als` format is undocumented and version-specific. This project's rule: **only facts verified empirically against real files on the developer's machine go in the list.** Do not add from training data or plausible inference. The list, with the evidence, is in [CLAUDE.md](../../CLAUDE.md); this page is a map to it, not a second copy. CLAUDE.md is the source of truth if the two ever disagree.

## Where to look in CLAUDE.md

| Section | What it covers |
| --- | --- |
| Verified facts: Container | `.als` is gzipped XML; Live 12.4.5 also opens plain XML; lxml re-serialising is safe. |
| Verified facts: Ableton's own formatting | What `write_als.py` matches out of caution (declaration, tab indent, `<Foo />`). |
| Verified facts: Structure | Track elements, names, routing strings, volume as linear gain, pan, device nesting. |
| Devices come from Live's own preset files | The `.adv` library, and how inserting a device works. |
| Vendor sets (read only) | Live 8 vendor sets (Washed `MultiTrack.als`): tracks, locators, Live 8 versus Live 12 sample paths and tempo. Read by `app/vendor_set.py`. |
| Hard invariants | The seven rules that, if broken, corrupt a set (pointee ID pool, `NextPointeeId`, send lists, track IDs, routing strings, the `<Ableton>` header, `LomId`). |
| Track layout rules | Part tracks, `import_part`, No Input, per-song mix in clips (and per-song faders through the app). These are app rules, not file facts; see [part-tracks-and-imports.md](part-tracks-and-imports.md). |
| Working notes | What was learned from the live Live API (for example, transposing an unwarped clip also changes its speed, so transposed clips are warped with Complex Pro and pinned 1:1). |

## The short version

- Volume is a linear gain factor, not dB: `gain = 10^(dB/20)`; 0 dB is `1`.
- Never synthesize the `<Ableton ... Creator>` header; copy it from the template.
- Pointee IDs span every tag that is `Pointee` or ends with `Target`; match by suffix, never by a list.
- Send lists must match the return tracks exactly, or Live crashes on load.
- Hardware output routing and stereo input pairs are **not known** yet. Only `AudioOut/Main` and mono inputs are verified.

## Learning a new fact

1. Generate a set with the thing you are unsure about.
2. Open it in Live, change that one thing by hand, save.
3. Diff Live's save against ours. The difference is the answer.
4. Keep the saved probe in `templates/` as a fixture, and add the fact to CLAUDE.md.

`templates/` is read-only; never mutate it. See [Testing and development](testing-and-development.md) and [File renderer](file-renderer.md).

# The idea

**Describe your Sunday. Get a working Ableton session.**

## The problem

Somewhere in every church running backing tracks, there's a volunteer who spends
Saturday night building an Ableton session. Four songs, each at its own tempo. A
click track the drummer can hear and the congregation can't. Stems for the parts
nobody's playing this week. Five in-ear mixes, each routed to the right output.
Inputs mapped so the vocal mic lands on the vocal channel.

It takes hours, it happens every single week, and it's the same work every time
with different songs. The person doing it usually learned Ableton by watching
YouTube at 1.5x, and when they're out of town, the team does without.

That's the church that *has* someone. Most small churches don't. They don't run
tracks at all — not because they don't want to, but because the setup cost is a
wall and nobody on the team can climb it.

## What we're building

A conversation that ends in a finished session file.

The volunteer opens a chat and says what their Sunday looks like:

> "Four songs this week. Lead vocal and two backups, acoustic guitar DI, keys in
> stereo. Drummer needs click in his ears. Running an 8-input Scarlett."

We ask what we still need to know — which mics are on which inputs, who gets
in-ears — and then hand back a `.als` file. They double-click it. Ableton opens
with the tracks named, inputs patched, compression and reverb already on the
vocals, monitor sends built, and a scene per song at the right tempo.

No Ableton knowledge required. No template to download and modify. No JSON, no
config file, no terminal. **One click to a session that's ready to run.**

The second week is faster, because we remember their room. Same gear, same
in-ear setup, new setlist — and it takes one sentence.

## Why this works

The trick is that the AI never touches Ableton's file format.

Live's session format is undocumented, version-specific, and unforgiving — a
single wrong number and it refuses to open. Ask a language model to write that
XML and you get confident nonsense. So we don't.

Instead the model does the part it's genuinely good at: turning a messy human
description into a clear, structured description of a rig. It says *"lead vocal
on input 1, gentle compression, plate reverb"* — never *"Ratio 3.5, Attack
12ms."* Our renderer owns every piece of Ableton-specific knowledge, and it
builds the file from Live's own device library. The model literally cannot
produce a broken session, because it never touches the mechanism that could break
one.

That's also what makes it trustworthy enough to hand to a volunteer twenty
minutes before a service.

## Where it goes

Right now the loop is: describe, generate, open. The next step is live — you have
the session open, you say "more reverb on the lead vocal," and it changes in
front of you. That's an open technical question rather than a promise (see
`issue.md`), but it's the version we're building toward.

Further out, the interesting part isn't the file at all. It's that a small church
with no audio engineer gets the same starting point as one with a paid staff
position, for the cost of a conversation.

## Where we are

Built for the 2026 Gloo AI Hackathon, Ministry Resourcing track — solving real
operational problems for ministries.

The Ableton file format has been reverse-engineered and the round-trip is proven:
files we generate open in Live 12.4.5 without errors. The renderer that turns a
rig description into a full session is next. Technical detail lives in
`CLAUDE.md`.

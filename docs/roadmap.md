# Sound Lab — roadmap

Sound library analysis and film sound tools, on the home AI tower. Decision record:
`home-infra/docs/decisions/0011-sound-lab.md`. Each phase ends with something usable and a
measurement; nothing modifies the original files.

## Phase 0 — Inventory (CPU only, read-only) — *started 2026-11-20*

- Mount the sound shares of the NAS **read-only** on the tower (dedicated read-only NAS account).
- `soundlab inventory`: walk the shares, read **headers only** (WAV/BWF/RF64/AIFF chunks;
  ffprobe for compressed audio and video): format, sample rate, bit depth, channels, duration,
  BWF description / originator / date, iXML and marker presence, size, dates.
- Quick fingerprint (size + first and last 64 KiB) to find **duplicate candidates** without
  reading whole files over the slow NAS; full SHA-256 during the migration copy.
- Resumable: a file already inventoried with the same size and date is skipped.
- **Report**: per share and top folders — files, TB, hours of audio, formats, sample rates,
  channel layouts, long files (rush candidates), files with embedded descriptions, duplicates.
- Output: `catalog.sqlite` on the tower's NVMe, `inventory-report.md`.
- **Decides**: the size of the new NAS; the folder tree to label in phase 1.

## Phase 1 — The app, first version: library and folder labelling

- Web app (React + wavesurfer.js, Python API), VPN only: browse the catalog, play previews
  (FLAC/Opus generated on the tower), waveforms precomputed (instant on hour-long files).
- **Folder labelling**: each folder with a proposed **UCS** category (from its path, by a
  local model); Roland accepts or corrects — hundreds of decisions label tens of thousands of
  files. Origin and rights per folder (`own`, `library:<name>`, `film:<title>`).
- **Descriptor vocabulary** agreed with Roland: UCS category/subcategory + perspective,
  interior/exterior, material, intensity, sync vs ambience, format (mono, stereo, M/S,
  ambisonics…).
- Keyboard-first: space, J/K/L, I/O, Enter accept, digits for categories, Tab next.

## Phase 2 — Derushing (GPU)

- Speech detection and **slate transcription** (Whisper, FR/EN): spoken slates become
  descriptions and labels of the takes that follow.
- Segmentation (silences, onsets), take grouping, segment classes (sound of interest, speech,
  handling, wind, bumps, noise floor), quality (clipping, noise floor, wind, loudness), best
  take proposal.
- **Derush screen**: waveform + spectrogram + proposed regions; adjust boundaries, accept or
  reject, relabel. Accepted regions become clips in the catalog.
- **Export**: render accepted regions from the originals (same rate, bit depth, channels),
  UCS names, BWF/iXML descriptions, into an output folder; or a **Reaper project** per rush
  with the regions placed.
- Reference: a few rushes derushed by Roland → boundary and label accuracy.

## Phase 3 — Listening and search (GPU)

- Audio embeddings (CLAP-type) for every clip; sound tags; short descriptions by a local
  audio-language model; label propagation from labelled folders to similar sounds.
- Index in **LanceDB** (one dataset per rights space), benchmarked against pgvector.
- **Search**: by text ("heavy metal door, slow, reverberant, interior") and **by similar
  sound**; filters (category, origin, format, duration, quality).
- **Triage**: uncertain predictions first, one key to confirm or correct (active learning).
- **Reference set**: ~500 sounds labelled by Roland across categories → tagging accuracy,
  measured at each model change. Fine-tune the recognition model on the UCS categories.

## Phase 4 — Picture analysis and spotting

- For a film sequence (picture + reference sound): shot cuts, actions and events with
  frame-accurate timecodes (local vision model), reference sound analysis (dialogue,
  ambiences).
- **Spotting**: each event → candidate sounds from the library (text and similarity search),
  aligned to the frame; layers (hard FX, foley, ambiences).
- **Timeline**: OpenTimelineIO → **AAF** (Pro Tools) or Reaper project; Roland finishes the
  edit in his software.
- If sessions / AAF / EDL / stems of past films exist with their picture: they become
  **examples to learn spotting** and the **benchmark** of the automatic timelines.

## Phase 5 — Synthesis

- **Video-to-audio** (MMAudio-type): synchronised foley from a sequence.
- **Text-to-audio** (Stable Audio Open-type): **LoRA trained on rights-cleared sounds only**
  (own recordings, libraries whose licence allows it), with descriptions from phases 1–3.
- Generated sounds land in the timeline as candidates, beside library sounds.
- Measured: listening tests by Roland (preference vs library sounds), GPU time and energy.

## Cross-cutting

- Originals never modified; results as files + rebuildable indexes; backups of the catalog
  and labels (restic).
- One GPU job at a time (tower GPU lock), batches at night; energy in the "Cloud vs on-prem"
  dashboard.
- Rights tracked per file; generative datasets built only from cleared origins.
- The NAS migration (decision to come) copies with full checksums and runs phase 2–3 jobs on
  the new copy, not on the old QNAP.

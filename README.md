# Sound Lab

Tools to analyse, derush, label and search a professional sound library, and — later — to
build sound-editing timelines and synthesise sound effects for feature-film sequences. Runs
at home, on the AI tower (RTX 3090), with local models only. Original files are **never
modified**.

- Decision record: `home-infra/docs/decisions/0011-sound-lab.md`
- Roadmap: [docs/roadmap.md](docs/roadmap.md)

## Phase 0: inventory

```
python -m soundlab inventory --root /mnt/nas/SonoFilms --root /mnt/nas/SonoCD \
       --catalog /srv/sound-lab/catalog.sqlite
python -m soundlab report --catalog /srv/sound-lab/catalog.sqlite > inventory-report.md
```

Reads file **headers only** (WAV / BWF / RF64 / AIFF chunks natively; ffprobe for other audio
and video), never the whole file. Resumable: unchanged files are skipped.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

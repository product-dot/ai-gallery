# ai-gallery

Public visual library of tagged Instagram Reels for Venus Tech. Thumbnails and Instagram link-outs only — no video files are hosted.

## App

```bash
npm install
npm run dev
```

Open http://127.0.0.1:3000. Deploy by importing this repo into Vercel (Next.js is auto-detected). Do not add API keys as `NEXT_PUBLIC_` variables.

## Pipeline

Python scripts in this repo scrape, tag, and consolidate formats. After more videos are tagged:

```bash
python build_visual_library.py
```

That refreshes `data/library.json` and `public/thumbnails/`.

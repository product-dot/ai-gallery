# ai-gallery

Public visual library of tagged Instagram Reels for Venus Tech. Thumbnails and Instagram link-outs only — no video files are hosted.

## App

```bash
npm install
npm run dev
```

Open http://127.0.0.1:3000. Deploy by importing this repo into Vercel (Next.js is auto-detected). Do not add API keys as `NEXT_PUBLIC_` variables.

Live Accept/Reject stores decisions in MongoDB (`reel_reviews`). Set `MONGODB_URI` (and optional `MONGODB_DB`) in Vercel, and in Atlas allow access from `0.0.0.0/0` so serverless functions can connect.

## Pipeline

Python scripts in this repo scrape, tag, and consolidate formats. After more videos are tagged:

```bash
python build_visual_library.py
```

That refreshes `data/library.json` and `public/thumbnails/`.

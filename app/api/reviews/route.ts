import { NextRequest, NextResponse } from "next/server";
import {
  loadReviews,
  loadWeekReviews,
  reviewStorage,
  saveReviewSafe,
  type ReviewStatus,
} from "@/lib/reviews";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const USERNAME_RE = /^[A-Za-z0-9._]{1,64}$/;
const WEEK_RE = /^\d{4}-W\d{2}$/;
const SHORTCODE_RE = /^[A-Za-z0-9_-]+$/;

function payload(reviews: Record<string, unknown>) {
  const storage = reviewStorage();
  return {
    reviews,
    storage,
    persistent: storage === "mongo",
  };
}

export async function GET(request: NextRequest) {
  const weekId = (request.nextUrl.searchParams.get("week") || "").trim();
  if (!weekId) {
    if (reviewStorage() === "mongo") {
      return NextResponse.json({
        error: "Pass ?week=YYYY-Www",
        ...payload({}),
      }, { status: 400 });
    }
    return NextResponse.json(payload(loadReviews() as unknown as Record<string, unknown>));
  }
  if (!WEEK_RE.test(weekId)) {
    return NextResponse.json({ error: "Invalid week" }, { status: 400 });
  }
  return NextResponse.json(payload(await loadWeekReviews(weekId)));
}

export async function PUT(request: NextRequest) {
  if (process.env.VERCEL && reviewStorage() === "file") {
    return NextResponse.json(
      {
        error:
          "Live reviews need MongoDB. Add MONGODB_URI in Vercel (and MONGODB_DB if it is not in the URI).",
      },
      { status: 503 }
    );
  }

  let body: {
    weekId?: string;
    username?: string;
    status?: ReviewStatus;
    reason?: string;
    shortcode?: string;
  };
  try {
    body = (await request.json()) as typeof body;
  } catch {
    return NextResponse.json({ error: "Invalid JSON" }, { status: 400 });
  }

  const weekId = String(body.weekId || "").trim();
  const username = String(body.username || "").trim().toLowerCase();
  const shortcode = String(body.shortcode || "").trim();
  const status = body.status;
  if (!WEEK_RE.test(weekId) || !USERNAME_RE.test(username)) {
    return NextResponse.json({ error: "Invalid week or username" }, { status: 400 });
  }
  if (!SHORTCODE_RE.test(shortcode)) {
    return NextResponse.json({ error: "Invalid shortcode" }, { status: 400 });
  }
  if (status !== "accepted" && status !== "rejected") {
    return NextResponse.json({ error: "Invalid status" }, { status: 400 });
  }

  const saved = await saveReviewSafe(weekId, username, {
    status,
    reason: String(body.reason ?? ""),
    shortcode,
  });
  return NextResponse.json({
    ...saved,
    storage: reviewStorage(),
    persistent: reviewStorage() === "mongo",
  });
}

import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const SHORTCODE_RE = /^[A-Za-z0-9_-]+$/;
const OG_IMAGE_RE =
  /<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']|<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i;
const CACHE_CONTROL = "public, max-age=3600, s-maxage=3600";

function withCache(response: NextResponse): NextResponse {
  response.headers.set("Cache-Control", CACHE_CONTROL);
  response.headers.set("CDN-Cache-Control", CACHE_CONTROL);
  return response;
}

function decodeImageUrl(raw: string): string {
  return raw.replace(/&amp;/g, "&").replace(/&#x2F;/gi, "/");
}

async function ogImageFrom(url: string): Promise<string> {
  const response = await fetch(url, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      Accept: "text/html,application/xhtml+xml",
    },
    redirect: "follow",
  });
  if (!response.ok) return "";
  const html = await response.text();
  const match = html.match(OG_IMAGE_RE);
  const raw = match?.[1] || match?.[2] || "";
  return raw ? decodeImageUrl(raw) : "";
}

export async function GET(request: NextRequest) {
  const shortcode = (request.nextUrl.searchParams.get("shortcode") || "").trim();
  if (!shortcode) {
    return new NextResponse("Missing shortcode", { status: 400 });
  }
  if (!SHORTCODE_RE.test(shortcode)) {
    return new NextResponse("Invalid shortcode", { status: 400 });
  }

  try {
    const embedUrl = `https://www.instagram.com/reel/${shortcode}/embed/captioned/`;
    let imageUrl = await ogImageFrom(embedUrl);
    if (!imageUrl) {
      imageUrl = await ogImageFrom(
        `https://www.instagram.com/reel/${shortcode}/`
      );
    }
    if (!imageUrl) {
      return withCache(
        new NextResponse("Thumbnail not found", { status: 404 })
      );
    }

    const redirect = NextResponse.redirect(imageUrl, 307);
    return withCache(redirect);
  } catch {
    return new NextResponse("Failed to resolve thumbnail", { status: 500 });
  }
}

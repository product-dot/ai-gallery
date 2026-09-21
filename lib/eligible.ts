export type EligibleAccount = {
  username: string;
  followers: string;
  bio: string;
  external_url: string;
  niche_confirmed: string;
  selected_shortcode: string;
  caption: string;
  likesCount: string;
  commentsCount: string;
  viewCount: string;
  accessibility_caption: string;
  is_collab: string;
  selection_note: string;
  reels_checked_before_pass: string;
  exclusion_reason: string;
  profile_pic_url: string;
  thumbnail_url: string;
  thumbnail_path: string;
};

export function formatCount(value: string): string {
  const normalized = String(value || "").replace(/,/g, "").trim();
  const n = Number(normalized);
  return Number.isFinite(n) && normalized !== "" ? n.toLocaleString() : "—";
}

export function formatViews(value: string): string {
  const n = Number(String(value || "").replace(/,/g, "").trim());
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n >= 1_000_000) {
    const trimmed = (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1);
    return `${trimmed.replace(/\.0$/, "")}M`;
  }
  if (n >= 1_000) {
    const trimmed = (n / 1_000).toFixed(n >= 10_000 ? 0 : 1);
    return `${trimmed.replace(/\.0$/, "")}K`;
  }
  return n.toLocaleString();
}

export function reelUrl(shortcode: string): string {
  return `https://www.instagram.com/reel/${shortcode}/`;
}

export function reelsTabUrl(username: string): string {
  return `https://www.instagram.com/${username}/reels/`;
}

export type WeekSnapshot = {
  id: string;
  label: string;
  weekStart: string;
  weekEnd: string;
  savedAt: string;
  accounts: EligibleAccount[];
};

export function isoWeekId(now = new Date()): string {
  const tmp = new Date(
    Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
  );
  const day = tmp.getUTCDay() || 7;
  tmp.setUTCDate(tmp.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(tmp.getUTCFullYear(), 0, 1));
  const week = Math.ceil(
    ((tmp.getTime() - yearStart.getTime()) / 86400000 + 1) / 7
  );
  return `${tmp.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

const NICHE_LABELS: Record<string, string> = {
  seed_vetted: "Seed account",
  link_verified: "Adult link in bio",
  bio_keyword_only: "Bio keyword match",
};

export function selectionReason(account: EligibleAccount): string {
  const niche =
    NICHE_LABELS[account.niche_confirmed] || account.niche_confirmed || "";
  const note = account.selection_note || "";
  let reel = note.replace(/\|/g, " · ");
  if (note.includes("passed_caption_check")) {
    reel = "Highest-view non-collab reel passed caption check";
  } else if (note.includes("unverified_no_caption")) {
    reel = "Highest-view non-collab reel (no accessibility caption)";
  }
  return [niche, reel].filter(Boolean).join(" · ");
}

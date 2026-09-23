export type ReviewStatus = "accepted" | "rejected";

export type ReelReview = {
  status: ReviewStatus;
  reason: string;
  shortcode: string;
  username: string;
  updatedAt: string;
};

/** weekId -> reel shortcode -> review */
export type ReviewsFile = Record<string, Record<string, ReelReview>>;

export function reviewKey(account: {
  username?: string;
  selected_shortcode?: string;
}): string {
  const shortcode = String(account.selected_shortcode || "").trim();
  if (shortcode) return shortcode;
  return String(account.username || "").trim().toLowerCase();
}

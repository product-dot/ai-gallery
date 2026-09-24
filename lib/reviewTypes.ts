export type ReviewStatus = "accepted" | "rejected" | "pending";

export type ReelReview = {
  status: ReviewStatus;
  reason: string;
  shortcode: string;
  username: string;
  usedForCreator: boolean;
  creatorNames: string[];
  updatedAt: string;
};

export type ReviewPatch = {
  status?: ReviewStatus;
  reason?: string;
  usedForCreator?: boolean;
  creatorNames?: string[];
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

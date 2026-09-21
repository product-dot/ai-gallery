"use client";

import { useMemo, useState } from "react";
import {
  formatCount,
  formatViews,
  reelsTabUrl,
  reelUrl,
  selectionReason,
  type EligibleAccount,
} from "@/lib/eligible";

function ReelThumb({ account }: { account: EligibleAccount }) {
  const shortcode = account.selected_shortcode;
  const [failed, setFailed] = useState(!shortcode);

  if (failed) {
    return (
      <div className="placeholder">{formatViews(account.viewCount)} views</div>
    );
  }
  return (
    <img
      src={`/api/thumbnail?shortcode=${encodeURIComponent(shortcode)}`}
      alt={`@${account.username}`}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}

export default function VisualLibrary({
  accounts,
}: {
  accounts: EligibleAccount[];
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return accounts;
    return accounts.filter((account) =>
      account.username.toLowerCase().includes(needle)
    );
  }, [accounts, query]);

  return (
    <div className="visual-library">
      <div className="filters">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter by username"
          aria-label="Filter by username"
        />
        <p className="count">
          {filtered.length} of {accounts.length} accounts
        </p>
      </div>
      <main className="grid">
        {filtered.length === 0 ? (
          <p className="empty">
            {accounts.length === 0
              ? "No accounts with a selected reel yet."
              : "No accounts match that username."}
          </p>
        ) : (
          filtered.map((account) => {
            const permalink = reelUrl(account.selected_shortcode);
            const reels = reelsTabUrl(account.username);
            return (
              <article className="card" key={account.username}>
                <a
                  className="thumb"
                  href={permalink}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ReelThumb account={account} />
                  <span className="views-badge">
                    {formatViews(account.viewCount)} views
                  </span>
                </a>
                <div className="body">
                  <a
                    className="username"
                    href={permalink}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    @{account.username}
                  </a>
                  <p className="views-stat">
                    {formatCount(account.viewCount)} views
                  </p>
                  <p className="followers">
                    {formatCount(account.followers)} followers
                  </p>
                  <p className="reason">{selectionReason(account)}</p>
                  {account.caption ? (
                    <p className="caption">{account.caption}</p>
                  ) : (
                    <p className="caption muted">No caption</p>
                  )}
                  <p className="stats">
                    <span>{formatCount(account.likesCount)} likes</span>
                    <span>{formatCount(account.commentsCount)} comments</span>
                  </p>
                  <a
                    className="reels-link"
                    href={reels}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    View @{account.username}&apos;s Reels
                  </a>
                </div>
              </article>
            );
          })
        )}
      </main>
    </div>
  );
}

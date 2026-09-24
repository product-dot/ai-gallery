"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  formatCount,
  formatViews,
  reelsTabUrl,
  reelUrl,
  selectionReason,
  type EligibleAccount,
} from "@/lib/eligible";
import {
  reviewKey,
  type ReelReview,
  type ReviewPatch,
} from "@/lib/reviewTypes";

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

function ReasonBox({
  username,
  value,
  onSave,
}: {
  username: string;
  value: string;
  onSave: (reason: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(value);
  }, [value]);

  useEffect(() => {
    if (draft === value) return;
    const timer = window.setTimeout(() => onSave(draft), 450);
    return () => window.clearTimeout(timer);
  }, [draft, value, onSave]);

  return (
    <label className="reason-box">
      <span>Rejection reason</span>
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onFocus={() => {
          focused.current = true;
        }}
        onBlur={() => {
          focused.current = false;
          if (draft !== value) onSave(draft);
        }}
        placeholder="Why is this reel not suitable?"
        rows={3}
        aria-label={`Rejection reason for @${username}`}
      />
    </label>
  );
}

function CreatorBox({
  username,
  usedForCreator,
  creatorNames,
  onSave,
}: {
  username: string;
  usedForCreator: boolean;
  creatorNames: string[];
  onSave: (usedForCreator: boolean, creatorNames: string[]) => void;
}) {
  const [used, setUsed] = useState(usedForCreator);
  const [names, setNames] = useState(
    creatorNames.length > 0 ? creatorNames : [""]
  );
  const focused = useRef(false);

  useEffect(() => {
    if (!focused.current) {
      setUsed(usedForCreator);
      setNames(creatorNames.length > 0 ? creatorNames : [""]);
    }
  }, [usedForCreator, creatorNames]);

  useEffect(() => {
    const sameUsed = used === usedForCreator;
    const sameNames =
      names.map((name) => name.trim()).filter(Boolean).join("\n") ===
      creatorNames.map((name) => name.trim()).filter(Boolean).join("\n");
    if (sameUsed && sameNames) return;
    const timer = window.setTimeout(() => onSave(used, names), 450);
    return () => window.clearTimeout(timer);
  }, [used, names, usedForCreator, creatorNames, onSave]);

  return (
    <div className="creator-box">
      <label className="creator-used">
        <input
          type="checkbox"
          checked={used}
          onChange={(event) => {
            const next = event.target.checked;
            setUsed(next);
            onSave(next, names);
          }}
        />
        Used for creator
      </label>
      {names.map((name, index) => (
        <div className="creator-row" key={`creator-${index}`}>
          <input
            type="text"
            value={name}
            placeholder="Creator name"
            aria-label={`Creator name ${index + 1} for @${username}`}
            onFocus={() => {
              focused.current = true;
            }}
            onBlur={() => {
              focused.current = false;
              onSave(used, names);
            }}
            onChange={(event) => {
              const next = [...names];
              next[index] = event.target.value;
              setNames(next);
            }}
          />
          {names.length > 1 ? (
            <button
              type="button"
              className="creator-remove"
              onClick={() => {
                const next = names.filter((_, current) => current !== index);
                setNames(next);
                onSave(used, next);
              }}
            >
              Remove
            </button>
          ) : null}
        </div>
      ))}
      <button
        type="button"
        className="creator-add"
        onClick={() => setNames([...names, ""])}
      >
        Add another creator
      </button>
    </div>
  );
}

export default function VisualLibrary({
  accounts,
  reviews,
  mode,
  onReview,
}: {
  accounts: EligibleAccount[];
  reviews: Record<string, ReelReview>;
  mode: "queue" | "accepted" | "rejected";
  onReview: (account: EligibleAccount, patch: ReviewPatch) => void;
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
              ? mode === "rejected"
                ? "No rejected reels this week."
                : mode === "accepted"
                  ? "No accepted reels this week."
                  : "No accounts with a selected reel yet."
              : "No accounts match that username."}
          </p>
        ) : (
          filtered.map((account) => {
            const permalink = reelUrl(account.selected_shortcode);
            const reels = reelsTabUrl(account.username);
            const review = reviews[reviewKey(account)];
            const status = review?.status || "pending";
            return (
              <article
                className={
                  status === "accepted"
                    ? "card card-accepted"
                    : status === "rejected"
                      ? "card card-rejected"
                      : "card"
                }
                key={reviewKey(account)}
              >
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
                  <div className="review-actions">
                    <button
                      type="button"
                      className={
                        status === "accepted"
                          ? "review-btn accept active"
                          : "review-btn accept"
                      }
                      onClick={() =>
                        onReview(account, {
                          status:
                            status === "accepted" ? "pending" : "accepted",
                        })
                      }
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      className={
                        status === "rejected"
                          ? "review-btn reject active"
                          : "review-btn reject"
                      }
                      onClick={() =>
                        onReview(account, {
                          status: "rejected",
                          reason: review?.reason || "",
                        })
                      }
                    >
                      Reject
                    </button>
                  </div>
                  {mode === "rejected" ? (
                    <ReasonBox
                      username={account.username}
                      value={review?.reason || ""}
                      onSave={(reason) =>
                        onReview(account, { status: "rejected", reason })
                      }
                    />
                  ) : null}
                  {mode === "accepted" ? (
                    <CreatorBox
                      username={account.username}
                      usedForCreator={Boolean(review?.usedForCreator)}
                      creatorNames={review?.creatorNames || []}
                      onSave={(usedForCreator, creatorNames) =>
                        onReview(account, {
                          status: "accepted",
                          usedForCreator,
                          creatorNames,
                        })
                      }
                    />
                  ) : null}
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

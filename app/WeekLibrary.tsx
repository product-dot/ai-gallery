"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import VisualLibrary, { CopyButton } from "./VisualLibrary";
import {
  isoWeekId,
  reelUrl,
  type EligibleAccount,
  type WeekSnapshot,
} from "@/lib/eligible";
import {
  reviewKey,
  type ReelReview,
  type ReviewPatch,
  type ReviewStatus,
} from "@/lib/reviewTypes";

type Pane = "all" | "accepted" | "rejected" | "unseen";

function statusOf(
  reviews: Record<string, ReelReview>,
  account: EligibleAccount
): ReviewStatus {
  return reviews[reviewKey(account)]?.status || "pending";
}

function dayKeyFromIso(iso?: string): string {
  if (!iso) return "unknown";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "unknown";
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function todayKey(): string {
  return dayKeyFromIso(new Date().toISOString());
}

function formatAcceptedDay(key: string): string {
  if (key === "unknown") return "Unknown day";
  const [year, month, day] = key.split("-").map(Number);
  const label = new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  return key === todayKey() ? `Today · ${label}` : label;
}

function linksFor(accounts: EligibleAccount[]): string {
  return accounts
    .map((account) => reelUrl(account.selected_shortcode))
    .filter(Boolean)
    .join("\n");
}

export default function WeekLibrary({ weeks }: { weeks: WeekSnapshot[] }) {
  const currentId = isoWeekId();
  const defaultId = weeks.find((week) => week.id === currentId)?.id || weeks[0]?.id || "";
  const [activeId, setActiveId] = useState(defaultId);
  const [pane, setPane] = useState<Pane>("all");
  const [reviews, setReviews] = useState<Record<string, ReelReview>>({});
  const [persistent, setPersistent] = useState(true);
  const [showStorageWarning, setShowStorageWarning] = useState(false);
  const reviewsRef = useRef(reviews);
  const reviewsWeekRef = useRef(defaultId);
  reviewsRef.current = reviews;

  const active = useMemo(
    () => weeks.find((week) => week.id === activeId) || weeks[0],
    [weeks, activeId]
  );

  const loadWeekReviews = useCallback(async (weekId: string) => {
    const response = await fetch(`/api/reviews?week=${encodeURIComponent(weekId)}`);
    if (!response.ok) return;
    const data = (await response.json()) as {
      reviews?: Record<string, ReelReview>;
      persistent?: boolean;
    };
    const incoming = data.reviews || {};
    if (typeof data.persistent === "boolean") setPersistent(data.persistent);
    setReviews((prev) => {
      if (reviewsWeekRef.current !== weekId) {
        reviewsWeekRef.current = weekId;
        return incoming;
      }
      const next = { ...incoming };
      for (const [key, review] of Object.entries(prev)) {
        const theirs = next[key];
        if (!theirs || review.updatedAt > theirs.updatedAt) {
          next[key] = review;
        }
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (!active?.id) return;
    void loadWeekReviews(active.id);
    const timer = window.setInterval(() => {
      void loadWeekReviews(active.id);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [active?.id, loadWeekReviews]);

  useEffect(() => {
    const host = window.location.hostname;
    const local = host === "localhost" || host === "127.0.0.1";
    setShowStorageWarning(!persistent && !local);
  }, [persistent]);

  const onReview = useCallback(
    (account: EligibleAccount, patch: ReviewPatch) => {
      if (!active?.id) return;
      const key = reviewKey(account);
      const prev = reviewsRef.current[key];
      const status = patch.status || prev?.status || "pending";
      const next: ReelReview = {
        status,
        reason:
          status === "accepted"
            ? ""
            : patch.reason ?? prev?.reason ?? "",
        shortcode: account.selected_shortcode,
        username: account.username.toLowerCase(),
        usedForCreator:
          status === "rejected"
            ? false
            : patch.usedForCreator ?? prev?.usedForCreator ?? false,
        creatorNames: patch.creatorNames ?? prev?.creatorNames ?? [],
        updatedAt: new Date().toISOString(),
      };
      setReviews((current) => ({ ...current, [key]: next }));
      void fetch("/api/reviews", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          weekId: active.id,
          username: account.username.toLowerCase(),
          shortcode: account.selected_shortcode,
          status: next.status,
          reason: next.reason,
          usedForCreator: next.usedForCreator,
          creatorNames: next.creatorNames,
        }),
      })
        .then((response) => (response.ok ? response.json() : null))
        .then((data: { week?: Record<string, ReelReview> } | null) => {
          if (data?.week) setReviews(data.week);
        })
        .catch(() => {
          /* keep optimistic state; poll will refetch */
        });
    },
    [active?.id]
  );

  if (!active) {
    return (
      <p className="empty">No weekly selections saved yet.</p>
    );
  }

  const acceptedAccounts = active.accounts.filter(
    (account) => statusOf(reviews, account) === "accepted"
  );
  const rejectedAccounts = active.accounts.filter(
    (account) => statusOf(reviews, account) === "rejected"
  );
  const unseenAccounts = active.accounts.filter(
    (account) => statusOf(reviews, account) === "pending"
  );
  const acceptedByDay = (() => {
    const groups = new Map<string, EligibleAccount[]>();
    for (const account of acceptedAccounts) {
      const key = dayKeyFromIso(reviews[reviewKey(account)]?.updatedAt);
      const list = groups.get(key) || [];
      list.push(account);
      groups.set(key, list);
    }
    return [...groups.entries()].sort(([left], [right]) => {
      if (left === "unknown") return 1;
      if (right === "unknown") return -1;
      return right.localeCompare(left);
    });
  })();
  const visible =
    pane === "accepted"
      ? acceptedAccounts
      : pane === "rejected"
        ? rejectedAccounts
        : pane === "unseen"
          ? unseenAccounts
          : active.accounts;
  const acceptedLinks = linksFor(acceptedAccounts);

  return (
    <div className="week-library">
      {showStorageWarning ? (
        <p className="review-warning">
          Reviews will not persist on the live site until MongoDB is
          connected (MONGODB_URI).
        </p>
      ) : null}
      <div className="week-tabs" role="tablist" aria-label="Weekly selections">
        {weeks.map((week) => {
          const selected = week.id === active.id;
          const title =
            week.id === currentId
              ? `This week · ${week.label}`
              : week.label;
          return (
            <button
              key={week.id}
              type="button"
              role="tab"
              aria-selected={selected}
              className={selected ? "week-tab active" : "week-tab"}
              onClick={() => {
                if (week.id !== active.id) {
                  reviewsWeekRef.current = week.id;
                  setReviews({});
                  setPane("all");
                }
                setActiveId(week.id);
              }}
            >
              {title}
              <span className="week-tab-count">{week.accounts.length}</span>
            </button>
          );
        })}
      </div>
      <div className="week-panel">
        <div className="week-panes" role="tablist" aria-label={`${active.label} review status`}>
          <button
            type="button"
            role="tab"
            aria-selected={pane === "all"}
            className={pane === "all" ? "week-tab active" : "week-tab"}
            onClick={() => setPane("all")}
          >
            All
            <span className="week-tab-count">{active.accounts.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={pane === "accepted"}
            className={
              pane === "accepted"
                ? "week-tab week-tab-accepted active"
                : "week-tab week-tab-accepted"
            }
            onClick={() => setPane("accepted")}
          >
            Accepted
            <span className="week-tab-count">{acceptedAccounts.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={pane === "rejected"}
            className={
              pane === "rejected"
                ? "week-tab week-tab-rejected active"
                : "week-tab week-tab-rejected"
            }
            onClick={() => setPane("rejected")}
          >
            Rejected
            <span className="week-tab-count">{rejectedAccounts.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={pane === "unseen"}
            className={pane === "unseen" ? "week-tab active" : "week-tab"}
            onClick={() => setPane("unseen")}
          >
            Not seen
            <span className="week-tab-count">{unseenAccounts.length}</span>
          </button>
        </div>
        <div className="accepted-links">
          <div className="accepted-links-head">
            <label htmlFor="accepted-links">Accepted reel links this week</label>
            <span>{acceptedAccounts.length} selected</span>
            <CopyButton text={acceptedLinks} label="Copy all links" />
          </div>
          <textarea
            id="accepted-links"
            readOnly
            value={acceptedLinks}
            rows={Math.min(8, Math.max(3, acceptedAccounts.length || 3))}
            placeholder="Accept a reel to add its Instagram link here, one per line."
          />
        </div>
        {pane === "accepted" ? (
          acceptedByDay.length === 0 ? (
            <VisualLibrary
              key={`${active.id}-accepted-empty`}
              accounts={[]}
              reviews={reviews}
              mode="accepted"
              onReview={onReview}
            />
          ) : (
            acceptedByDay.map(([day, accounts]) => (
              <section key={day} className="accepted-day">
                <div className="accepted-day-head">
                  <div>
                    <h2>{formatAcceptedDay(day)}</h2>
                    <p>
                      {accounts.length} accepted
                      {day === todayKey()
                        ? " · copy these if you have not used them yet"
                        : " · already copied on this day"}
                    </p>
                  </div>
                  <CopyButton
                    text={linksFor(accounts)}
                    label={`Copy ${accounts.length} links`}
                  />
                </div>
                <VisualLibrary
                  key={`${active.id}-accepted-${day}`}
                  accounts={accounts}
                  reviews={reviews}
                  mode="accepted"
                  onReview={onReview}
                />
              </section>
            ))
          )
        ) : (
          <>
            <VisualLibrary
              key={`${active.id}-${pane}`}
              accounts={visible}
              reviews={reviews}
              mode={pane === "rejected" ? "rejected" : "queue"}
              onReview={onReview}
            />
          </>
        )}
      </div>
    </div>
  );
}

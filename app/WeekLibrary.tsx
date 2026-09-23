"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import VisualLibrary from "./VisualLibrary";
import {
  isoWeekId,
  type EligibleAccount,
  type WeekSnapshot,
} from "@/lib/eligible";
import { reviewKey, type ReelReview, type ReviewStatus } from "@/lib/reviewTypes";

export default function WeekLibrary({ weeks }: { weeks: WeekSnapshot[] }) {
  const currentId = isoWeekId();
  const defaultId = weeks.find((week) => week.id === currentId)?.id || weeks[0]?.id || "";
  const [activeId, setActiveId] = useState(defaultId);
  const [pane, setPane] = useState<"queue" | "rejected">("queue");
  const [reviews, setReviews] = useState<Record<string, ReelReview>>({});
  const [persistent, setPersistent] = useState(true);
  const [showStorageWarning, setShowStorageWarning] = useState(false);

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
    (account: EligibleAccount, status: ReviewStatus, reason = "") => {
      if (!active?.id) return;
      const key = reviewKey(account);
      const next: ReelReview = {
        status,
        reason: status === "rejected" ? reason : "",
        shortcode: account.selected_shortcode,
        username: account.username.toLowerCase(),
        updatedAt: new Date().toISOString(),
      };
      setReviews((prev) => ({ ...prev, [key]: next }));
      void fetch("/api/reviews", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          weekId: active.id,
          username: account.username.toLowerCase(),
          status,
          reason: next.reason,
          shortcode: account.selected_shortcode,
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

  const rejectedCount = active.accounts.filter(
    (account) => reviews[reviewKey(account)]?.status === "rejected"
  ).length;
  const visible =
    pane === "rejected"
      ? active.accounts.filter(
          (account) => reviews[reviewKey(account)]?.status === "rejected"
        )
      : active.accounts.filter(
          (account) => reviews[reviewKey(account)]?.status !== "rejected"
        );

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
          const selected = pane === "queue" && week.id === active.id;
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
                setActiveId(week.id);
                setPane("queue");
              }}
            >
              {title}
              <span className="week-tab-count">{week.accounts.length}</span>
            </button>
          );
        })}
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
          <span className="week-tab-count">{rejectedCount}</span>
        </button>
      </div>
      <VisualLibrary
        key={`${active.id}-${pane}`}
        accounts={visible}
        reviews={reviews}
        mode={pane}
        onReview={onReview}
      />
    </div>
  );
}

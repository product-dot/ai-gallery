"use client";

import { useMemo, useState } from "react";
import VisualLibrary from "./VisualLibrary";
import { isoWeekId, type WeekSnapshot } from "@/lib/eligible";

export default function WeekLibrary({ weeks }: { weeks: WeekSnapshot[] }) {
  const currentId = isoWeekId();
  const defaultId = weeks.find((week) => week.id === currentId)?.id || weeks[0]?.id || "";
  const [activeId, setActiveId] = useState(defaultId);
  const active = useMemo(
    () => weeks.find((week) => week.id === activeId) || weeks[0],
    [weeks, activeId]
  );

  if (!active) {
    return (
      <p className="empty">No weekly selections saved yet.</p>
    );
  }

  return (
    <div className="week-library">
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
              onClick={() => setActiveId(week.id)}
            >
              {title}
              <span className="week-tab-count">{week.accounts.length}</span>
            </button>
          );
        })}
      </div>
      <VisualLibrary key={active.id} accounts={active.accounts} />
    </div>
  );
}

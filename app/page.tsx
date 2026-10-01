import WeekLibrary from "./WeekLibrary";
import { loadWeekSnapshots } from "@/lib/loadEligible";
import { isoWeekId } from "@/lib/eligible";

export const dynamic = "force-dynamic";

export default function HomePage() {
  const weeks = loadWeekSnapshots();
  const current = weeks.find((week) => week.id === isoWeekId()) || weeks[0];
  const count = current?.accounts.length ?? 0;
  return (
    <>
      <header>
        <h1>Eligible accounts</h1>
        <p className="lede">
          Weekly batches of {count || 40} selected reel links. Last week stays
          in its own tab; this week is only that week&apos;s new finds.
          Accepted and Rejected stay under the selected week. Photo or
          username opens that reel; the text link opens the creator&apos;s
          Reels tab. Thumbnails are resolved live and are not stored.
        </p>
      </header>
      <WeekLibrary weeks={weeks} />
    </>
  );
}

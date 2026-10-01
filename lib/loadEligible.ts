import { readdirSync, readFileSync } from "fs";
import path from "path";
import { isoWeekId, type EligibleAccount, type WeekSnapshot } from "./eligible";

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field);
      field = "";
      if (row.some((cell) => cell.length > 0)) rows.push(row);
      row = [];
    } else {
      field += char;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

export function loadEligibleAccounts(): EligibleAccount[] {
  const csvPath = path.join(process.cwd(), "eligible_accounts.csv");
  let text = "";
  try {
    text = readFileSync(csvPath, "utf8");
  } catch {
    return [];
  }
  const rows = parseCsv(text.replace(/^\uFEFF/, ""));
  if (rows.length < 2) return [];
  const headers = rows[0].map((header) => header.trim());
  return rows
    .slice(1)
    .map((cols) => {
      const record: Record<string, string> = {};
      headers.forEach((header, index) => {
        record[header] = cols[index] ?? "";
      });
      return record as EligibleAccount;
    })
    .filter((row) => row.selected_shortcode && !row.exclusion_reason);
}

function emptyAccount(): EligibleAccount {
  return {
    username: "",
    followers: "",
    bio: "",
    external_url: "",
    niche_confirmed: "",
    selected_shortcode: "",
    caption: "",
    likesCount: "",
    commentsCount: "",
    viewCount: "",
    accessibility_caption: "",
    is_collab: "",
    selection_note: "",
    reels_checked_before_pass: "",
    exclusion_reason: "",
    profile_pic_url: "",
    thumbnail_url: "",
    thumbnail_path: "",
    us_signal: "",
    us_signal_source: "",
  };
}

function usernameOf(account: EligibleAccount): string {
  return String(account.username || "").trim().toLowerCase();
}

function priorWeekUsage(
  weeks: WeekSnapshot[],
  currentId: string
): { usernames: Set<string>; shortcodes: Set<string> } {
  const usernames = new Set<string>();
  const shortcodes = new Set<string>();
  for (const week of weeks) {
    if (week.id === currentId) continue;
    for (const account of week.accounts) {
      const user = usernameOf(account);
      if (user) usernames.add(user);
      const code = String(account.selected_shortcode || "").trim();
      if (code) shortcodes.add(code);
    }
  }
  return { usernames, shortcodes };
}

function isNewThisWeek(
  account: EligibleAccount,
  prior: { usernames: Set<string>; shortcodes: Set<string> }
): boolean {
  const code = String(account.selected_shortcode || "").trim();
  if (!code || prior.shortcodes.has(code)) return false;
  const user = usernameOf(account);
  if (user && prior.usernames.has(user)) return false;
  return true;
}

export function loadWeekSnapshots(): WeekSnapshot[] {
  const dir = path.join(process.cwd(), "data", "weeks");
  let names: string[] = [];
  try {
    names = readdirSync(dir).filter((name) => name.endsWith(".json"));
  } catch {
    names = [];
  }

  const weeks = names
    .map((name) => {
      try {
        const raw = JSON.parse(readFileSync(path.join(dir, name), "utf8")) as {
          id?: string;
          label?: string;
          week_start?: string;
          week_end?: string;
          saved_at?: string;
          accounts?: Record<string, string>[];
        };
        const accounts = (raw.accounts || [])
          .map((row) => ({ ...emptyAccount(), ...row }))
          .filter((row) => row.selected_shortcode);
        return {
          id: raw.id || name.replace(/\.json$/, ""),
          label: raw.label || name.replace(/\.json$/, ""),
          weekStart: raw.week_start || "",
          weekEnd: raw.week_end || "",
          savedAt: raw.saved_at || "",
          accounts,
        } satisfies WeekSnapshot;
      } catch {
        return null;
      }
    })
    .filter((week): week is WeekSnapshot => week !== null)
    .sort((a, b) => (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));

  const live = loadEligibleAccounts();
  const currentId = isoWeekId();
  const prior = priorWeekUsage(weeks, currentId);
  const current = weeks.find((week) => week.id === currentId);
  const byShortcode = new Map<string, EligibleAccount>();
  for (const row of current?.accounts || []) {
    if (isNewThisWeek(row, prior)) byShortcode.set(row.selected_shortcode, row);
  }
  for (const row of live) {
    if (isNewThisWeek(row, prior)) byShortcode.set(row.selected_shortcode, row);
  }
  const thisWeekAccounts = [...byShortcode.values()];
  const others = weeks.filter((week) => week.id !== currentId);

  if (thisWeekAccounts.length > 0 || current) {
    const merged: WeekSnapshot = {
      id: currentId,
      label: current?.label || "This week",
      weekStart: current?.weekStart || "",
      weekEnd: current?.weekEnd || "",
      savedAt: current?.savedAt || "",
      accounts: thisWeekAccounts,
    };
    return [merged, ...others];
  }

  if (others.length > 0) return others;

  if (live.length === 0) return [];
  return [
    {
      id: currentId || "current",
      label: "This week",
      weekStart: "",
      weekEnd: "",
      savedAt: "",
      accounts: live.filter((row) => isNewThisWeek(row, prior)),
    },
  ];
}

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "fs";
import path from "path";
import { MongoClient, type Db } from "mongodb";
import type { ReelReview, ReviewsFile, ReviewStatus } from "./reviewTypes";

export type { ReelReview, ReviewsFile, ReviewStatus };

const REVIEWS_PATH = path.join(process.cwd(), "data", "reviews.json");
const COLLECTION = "reel_reviews";

type ReviewDoc = ReelReview & { weekId: string };

const globalMongo = globalThis as typeof globalThis & {
  _mongoClientPromise?: Promise<MongoClient>;
  _mongoIndexesReady?: boolean;
};

function mongoUri(): string {
  return (process.env.MONGODB_URI || "").trim();
}

function mongoDbName(): string {
  return (process.env.MONGODB_DB || "ai_gallery").trim() || "ai_gallery";
}

export function reviewStorage(): "mongo" | "file" {
  return mongoUri() ? "mongo" : "file";
}

async function getDb(): Promise<Db> {
  const uri = mongoUri();
  if (!uri) {
    throw new Error("Missing MONGODB_URI");
  }
  if (!globalMongo._mongoClientPromise) {
    const client = new MongoClient(uri);
    globalMongo._mongoClientPromise = client.connect();
  }
  const client = await globalMongo._mongoClientPromise;
  const db = client.db(mongoDbName());
  if (!globalMongo._mongoIndexesReady) {
    await db.collection(COLLECTION).createIndex(
      { weekId: 1, shortcode: 1 },
      { unique: true }
    );
    globalMongo._mongoIndexesReady = true;
  }
  return db;
}

export function normalizeCreatorNames(names: unknown): string[] {
  if (!Array.isArray(names)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const name of names) {
    const trimmed = String(name || "").trim();
    if (!trimmed) continue;
    const key = trimmed.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(trimmed);
    if (out.length >= 20) break;
  }
  return out;
}

function asReview(raw: unknown): ReelReview | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Partial<ReelReview>;
  if (
    row.status !== "accepted" &&
    row.status !== "rejected" &&
    row.status !== "pending"
  ) {
    return null;
  }
  return {
    status: row.status,
    reason: String(row.reason || ""),
    shortcode: String(row.shortcode || ""),
    username: String(row.username || ""),
    usedForCreator: Boolean(row.usedForCreator),
    creatorNames: normalizeCreatorNames(row.creatorNames),
    updatedAt: String(row.updatedAt || ""),
  };
}

export function loadReviews(): ReviewsFile {
  try {
    const raw = readFileSync(REVIEWS_PATH, "utf8");
    const parsed = JSON.parse(raw) as ReviewsFile;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function reviewsForWeek(
  all: ReviewsFile,
  weekId: string
): Record<string, ReelReview> {
  const week = all[weekId];
  return week && typeof week === "object" ? week : {};
}

async function loadWeekFromMongo(
  weekId: string
): Promise<Record<string, ReelReview>> {
  const db = await getDb();
  const docs = await db.collection<ReviewDoc>(COLLECTION).find({ weekId }).toArray();
  const week: Record<string, ReelReview> = {};
  for (const doc of docs) {
    const review = asReview(doc);
    const field = review?.shortcode || doc.shortcode;
    if (review && field) week[field] = review;
  }
  return week;
}

export async function loadWeekReviews(
  weekId: string
): Promise<Record<string, ReelReview>> {
  if (mongoUri()) return loadWeekFromMongo(weekId);
  return reviewsForWeek(loadReviews(), weekId);
}

function buildReview(
  username: string,
  patch: Partial<ReelReview> & { status: ReviewStatus },
  prev?: ReelReview
): ReelReview {
  const next: ReelReview = {
    status: patch.status,
    reason: patch.reason ?? prev?.reason ?? "",
    shortcode: patch.shortcode ?? prev?.shortcode ?? "",
    username: username || prev?.username || "",
    usedForCreator: patch.usedForCreator ?? prev?.usedForCreator ?? false,
    creatorNames: normalizeCreatorNames(
      patch.creatorNames ?? prev?.creatorNames ?? []
    ),
    updatedAt: new Date().toISOString(),
  };
  if (next.status === "accepted") {
    next.reason = "";
  }
  if (next.status === "rejected") {
    next.usedForCreator = false;
  }
  return next;
}

function fieldFor(username: string, shortcode: string): string {
  return shortcode.trim() || username;
}

let writeQueue: Promise<unknown> = Promise.resolve();

function enqueueWrite<T>(fn: () => T): Promise<T> {
  const run = writeQueue.then(fn, fn);
  writeQueue = run.then(
    () => undefined,
    () => undefined
  );
  return run;
}

function saveReviewFile(
  weekId: string,
  username: string,
  patch: Partial<ReelReview> & { status: ReviewStatus }
): { review: ReelReview; week: Record<string, ReelReview> } {
  const all = loadReviews();
  const week = { ...(all[weekId] || {}) };
  const field = fieldFor(username, String(patch.shortcode || ""));
  const next = buildReview(username, patch, week[field]);
  week[field] = next;
  all[weekId] = week;
  writeReviews(all);
  return { review: next, week };
}

export async function saveReviewSafe(
  weekId: string,
  username: string,
  patch: Partial<ReelReview> & { status: ReviewStatus }
): Promise<{ review: ReelReview; week: Record<string, ReelReview> }> {
  if (mongoUri()) {
    const db = await getDb();
    const shortcode = fieldFor(username, String(patch.shortcode || ""));
    const prevDoc = await db.collection<ReviewDoc>(COLLECTION).findOne({
      weekId,
      shortcode,
    });
    const review = buildReview(username, patch, asReview(prevDoc) || undefined);
    await db.collection<ReviewDoc>(COLLECTION).updateOne(
      { weekId, shortcode },
      { $set: { ...review, weekId, shortcode } },
      { upsert: true }
    );
    const week = await loadWeekFromMongo(weekId);
    return { review, week };
  }
  return enqueueWrite(() => saveReviewFile(weekId, username, patch));
}

function writeReviews(all: ReviewsFile): void {
  mkdirSync(path.dirname(REVIEWS_PATH), { recursive: true });
  const tmp = `${REVIEWS_PATH}.tmp`;
  writeFileSync(tmp, JSON.stringify(all, null, 2) + "\n", "utf8");
  renameSync(tmp, REVIEWS_PATH);
}

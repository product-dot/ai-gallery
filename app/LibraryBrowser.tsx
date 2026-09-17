"use client";

import { useMemo, useState } from "react";
import type { LibraryData, LibraryVideo } from "./types";

function formatCount(value: string): string {
  const n = Number(String(value).replace(/,/g, ""));
  return Number.isFinite(n) ? n.toLocaleString() : value || "—";
}

function thumbnailSrc(path: string): string {
  if (!path) return "";
  return path.startsWith("/") ? path : `/${path}`;
}

function matches(
  video: LibraryVideo,
  username: string,
  formatName: string
): boolean {
  if (username && video.username !== username) return false;
  if (formatName && video.consolidated_format !== formatName) return false;
  return true;
}

export default function LibraryBrowser({ data }: { data: LibraryData }) {
  const [username, setUsername] = useState("");
  const [formatName, setFormatName] = useState("");

  const formats = useMemo(
    () =>
      [...data.formats].sort(
        (a, b) => (b.example_count || 0) - (a.example_count || 0)
      ),
    [data.formats]
  );

  const usernames = useMemo(() => {
    const counts = new Map<string, number>();
    for (const video of data.videos) {
      counts.set(video.username, (counts.get(video.username) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [data.videos]);

  const sections = useMemo(() => {
    return formats
      .map((fmt) => ({
        fmt,
        videos: data.videos.filter(
          (video) =>
            video.consolidated_format === fmt.consolidated_format_name &&
            matches(video, username, formatName)
        ),
      }))
      .filter((section) => section.videos.length > 0);
  }, [data.videos, formatName, formats, username]);

  return (
    <>
      <div className="filters">
        <select
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          aria-label="Filter by username"
        >
          <option value="">All usernames</option>
          {usernames.map(([name, count]) => (
            <option key={name} value={name}>
              @{name} ({count})
            </option>
          ))}
        </select>
        <select
          value={formatName}
          onChange={(event) => setFormatName(event.target.value)}
          aria-label="Filter by format"
        >
          <option value="">All formats</option>
          {formats.map((fmt) => (
            <option
              key={fmt.consolidated_format_name}
              value={fmt.consolidated_format_name}
            >
              {fmt.consolidated_format_name} ({fmt.example_count || 0})
            </option>
          ))}
        </select>
      </div>
      <main>
        {sections.map(({ fmt, videos }) => (
          <section className="format" key={fmt.consolidated_format_name}>
            <h2>{fmt.consolidated_format_name}</h2>
            <div className="meta">
              <div>
                <b>Fixed structure:</b> {fmt.fixed_structure}
              </div>
              <div>
                <b>Variable elements:</b> {fmt.variable_elements}
              </div>
              <div>
                {videos.length} video{videos.length === 1 ? "" : "s"}
              </div>
            </div>
            <div className="grid">
              {videos.map((video) => (
                <article className="card" key={video.video_filename}>
                  {video.thumbnail ? (
                    <div className="thumb">
                      <img src={thumbnailSrc(video.thumbnail)} alt="" />
                    </div>
                  ) : (
                    <div className="thumb missing">No thumbnail</div>
                  )}
                  <div className="body">
                    <div className="user">@{video.username}</div>
                    {video.caption ? (
                      <div className="caption">{video.caption}</div>
                    ) : null}
                    <div className="field">
                      <b>Hook:</b> {video.hook}
                    </div>
                    <div className="field">
                      <b>Core action:</b> {video.core_action}
                    </div>
                    <div className="field">
                      <b>Payoff / CTA:</b> {video.payoff_or_cta}
                    </div>
                    <div className="stats">
                      <span>{formatCount(video.likesCount)} likes</span>
                      <span>{formatCount(video.videoViewCount)} views</span>
                    </div>
                    <a
                      className="btn"
                      href={video.permalink}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      View on Instagram
                    </a>
                  </div>
                </article>
              ))}
            </div>
          </section>
        ))}
      </main>
      {sections.length === 0 ? (
        <p className="empty">No videos match this filter.</p>
      ) : null}
    </>
  );
}

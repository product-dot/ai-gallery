import Link from "next/link";
import libraryData from "../../data/library.json";
import LibraryBrowser from "../LibraryBrowser";
import type { LibraryData } from "../types";

const data = libraryData as LibraryData;

export default function TaggedPage() {
  return (
    <>
      <header>
        <p className="nav">
          <Link href="/">Visual library</Link>
          <Link href="/tagged">Tagged formats</Link>
        </p>
        <h1>Venus Tech content library</h1>
        <p className="lede">
          Tagged Instagram Reels grouped by consolidated format. Thumbnails
          only; original posts open on Instagram.
        </p>
      </header>
      <LibraryBrowser data={data} />
    </>
  );
}

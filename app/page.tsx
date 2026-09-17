import libraryData from "../data/library.json";
import LibraryBrowser from "./LibraryBrowser";
import type { LibraryData } from "./types";

const data = libraryData as LibraryData;

export default function HomePage() {
  return (
    <>
      <header>
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

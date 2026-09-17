import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Venus Tech Content Library",
  description:
    "Tagged Instagram Reels grouped by consolidated format. Thumbnails only; original posts open on Instagram.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

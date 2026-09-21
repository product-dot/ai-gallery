import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Eligible accounts",
  description:
    "Weekly selected Instagram reel links. Public Instagram URLs only.",
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

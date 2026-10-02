import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Threshold",
  description: "Routine awareness for a family that lives far apart.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

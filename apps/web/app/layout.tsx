import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://forge-clipmind-control-center.karnaukhovartem20.chatgpt.site"),
  title: "Forge Control Center",
  description: "Autonomous Developer + QA Mission Control",
  openGraph: {
    title: "Forge Control Center",
    description: "Autonomous Developer + QA Mission Control",
    images: [{ url: "/og.png", width: 1680, height: 941, alt: "Forge Control Center mission dashboard" }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Forge Control Center",
    description: "Autonomous Developer + QA Mission Control",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

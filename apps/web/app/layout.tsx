import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./fonts.css";
import "./globals.css";
import "./reference-design.css";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.PUBLIC_APP_URL ?? "http://localhost:3000"),
  title: "Forge Control Center",
  icons: { icon: "/favicon.svg" },
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

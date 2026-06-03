/** @type {import('next').NextConfig} */
//
// Build modes:
//   - default (dev, `next dev`)        → dynamic; ignores `output`
//   - NEXT_OUTPUT=standalone            → Container App image (legacy)
//   - NEXT_OUTPUT=export                → Static bundle for Azure Static Web Apps
//
// Trailing slashes match SWA's default file layout, so `/sources` resolves
// to `sources/index.html` cleanly when navigationFallback rewrites apply.
const nextConfig = {
  reactStrictMode: true,
  output: process.env.NEXT_OUTPUT || undefined,
  trailingSlash: process.env.NEXT_OUTPUT === "export",
};

export default nextConfig;

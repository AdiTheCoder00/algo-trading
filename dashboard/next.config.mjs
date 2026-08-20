/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // No images, no fonts fetched at build time, no telemetry surface. The
  // dashboard is a local monitoring page for one operator, not a web product.
  poweredByHeader: false,

  // Dev only. Next blocks cross-origin requests for its own dev chunks, and it
  // treats 127.0.0.1 and localhost as different origins — so opening the page by
  // IP silently serves the shell with no JavaScript, which looks exactly like an
  // engine that is up but reporting nothing. Both spellings are allowed so the
  // failure cannot happen either way round.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;

// Cloudflare Worker — HF Bucket 代理
// 部署: https://dash.cloudflare.com → Workers & Pages → Create Worker
// 绑定自定义域名后，替换 app.py 里的 CDN_BASE

const HF_BASE = "https://huggingface.co";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 只代理 /buckets/ 路径，其他返回 404
    if (!url.pathname.startsWith("/buckets/")) {
      return new Response("Not Found", { status: 404 });
    }

    const target = `${HF_BASE}${url.pathname}${url.search}`;

    // 构建转发请求，保留 Range 头（视频拖进度条需要）
    const headers = new Headers();
    const range = request.headers.get("Range");
    if (range) headers.set("Range", range);

    const response = await fetch(target, { headers });

    // 构建响应，保留关键头
    const respHeaders = new Headers();
    const contentType = response.headers.get("Content-Type");
    const contentLength = response.headers.get("Content-Length");
    const contentRange = response.headers.get("Content-Range");
    const acceptRanges = response.headers.get("Accept-Ranges");

    if (contentType) respHeaders.set("Content-Type", contentType);
    if (contentLength) respHeaders.set("Content-Length", contentLength);
    if (contentRange) respHeaders.set("Content-Range", contentRange);
    if (acceptRanges) respHeaders.set("Accept-Ranges", acceptRanges);

    // CF 边缘缓存：图片 7 天，视频 1 天
    const ext = url.pathname.split(".").pop().toLowerCase();
    const isVideo = ["ts", "mp4", "mkv", "flv"].includes(ext);
    const maxAge = isVideo ? 86400 : 604800;
    respHeaders.set("Cache-Control", `public, max-age=${maxAge}`);
    // CORS（如果需要跨域）
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(response.body, {
      status: response.status,
      headers: respHeaders,
    });
  },
};

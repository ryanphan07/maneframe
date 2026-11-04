const MB = 1024 * 1024;

const formatSize = (mb) => {
  if (mb >= 1024) return `${Number((mb / 1024).toFixed(1))} GB`;
  return `${Math.round(mb)} MB`;
};

/** A user-facing message when the file is over the server's upload limit, else null. A missing limit means no check (the server still enforces it). */
export function uploadSizeError(fileBytes, limitMb) {
  if (!Number.isFinite(limitMb) || limitMb <= 0) return null;
  if (fileBytes <= limitMb * MB) return null;
  return `This clip is ${formatSize(fileBytes / MB)}; the limit is ${formatSize(limitMb)}. Trim it on your phone first.`;
}

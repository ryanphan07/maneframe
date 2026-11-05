// Request params and file name for the export controls. Defaults add nothing, so a plain export is the old request.
const secs = (value) => String(Math.round(value * 1000) / 1000);
const hasValue = (value) => value !== '' && value !== null && value !== undefined && Number.isFinite(Number(value));

export const exportParams = ({ captions = true, aspect = 'original', start = '', end = '' } = {}) => {
  const params = { captions: captions ? 1 : 0 };
  if (aspect !== 'original') params.aspect = aspect;
  if (hasValue(start)) params.start = Number(start);
  if (hasValue(end)) params.end = Number(end);
  return params;
};

export const exportFileName = (originalName, { aspect = 'original', start = '', end = '' } = {}) => {
  let suffix = '';
  if (aspect !== 'original') suffix += `_${aspect.replace(':', 'x')}`;
  if (hasValue(start) || hasValue(end)) suffix += `_${hasValue(start) ? secs(Number(start)) : '0'}-${hasValue(end) ? secs(Number(end)) : 'end'}s`;
  return `maneframe_${originalName.replace(/\.[^.]*$/, '')}${suffix}.mp4`;
};

// null when the range is usable (or empty), otherwise what is wrong with it
export const rangeProblem = ({ start = '', end = '' } = {}) => {
  if (hasValue(start) && Number(start) < 0) return 'Start cannot be negative.';
  if (hasValue(start) && hasValue(end) && Number(start) >= Number(end)) return 'Start must be before end.';
  return null;
};

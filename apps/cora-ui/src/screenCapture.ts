// Tier-2 Screen Vision (opt-in) — capture a SINGLE frame of a screen/window the user
// picks via the browser's getDisplayMedia prompt, then stop the track immediately.
// No continuous capture, no auto-capture: the OS picker is shown every time and the
// stream is torn down after one frame. Returns a JPEG data URL, or null if the user
// cancels / denies / the API is unavailable.

export async function captureScreenFrame(): Promise<string | null> {
  const md = navigator.mediaDevices as MediaDevices | undefined;
  if (!md || !md.getDisplayMedia) return null;
  let stream: MediaStream | null = null;
  try {
    stream = await md.getDisplayMedia({ video: true, audio: false });
    const video = document.createElement("video");
    video.srcObject = stream;
    video.muted = true;
    await new Promise<void>((res) => {
      video.onloadedmetadata = () => res();
    });
    await video.play();
    // Give the compositor a beat to paint a real frame before we grab it.
    await new Promise((r) => setTimeout(r, 120));
    const w = video.videoWidth || 1280;
    const h = video.videoHeight || 720;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(video, 0, 0, w, h);
    video.pause();
    return canvas.toDataURL("image/jpeg", 0.85);
  } catch {
    return null; // user cancelled / denied
  } finally {
    if (stream) stream.getTracks().forEach((t) => t.stop());
  }
}

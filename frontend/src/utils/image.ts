/**
 * 客户端图片压缩：缩放长边 + JPEG 重编码（顺带清除 EXIF）。
 *
 * 目的：省上传带宽/存储，并降低视觉模型的图片 token 成本。
 * 已足够小（长边≤MAX_EDGE 且体积≤MAX_BYTES）的图原样保留，避免二次损失。
 */

export interface CompressedImage {
  dataUrl: string
  name: string
  width: number
  height: number
  bytes: number
  compressed: boolean
}

const MAX_EDGE = 1536 // 长边上限（px）
const MAX_BYTES = 500 * 1024 // 阈值：超过则压缩
const QUALITY = 0.82

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('图片解码失败'))
    img.src = url
  })
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(new Error('图片读取失败'))
    r.readAsDataURL(blob)
  })
}

export async function compressImage(file: File): Promise<CompressedImage> {
  const asIs = async (): Promise<CompressedImage> => ({
    dataUrl: await blobToDataUrl(file),
    name: file.name || 'image.png',
    width: 0,
    height: 0,
    bytes: file.size,
    compressed: false
  })

  const url = URL.createObjectURL(file)
  try {
    const img = await loadImage(url)
    const w = img.naturalWidth || img.width
    const h = img.naturalHeight || img.height
    const long = Math.max(w, h)
    const scale = long > MAX_EDGE ? MAX_EDGE / long : 1
    const needResize = scale < 1
    const needBytes = file.size > MAX_BYTES
    if (!needResize && !needBytes) return asIs()

    const tw = Math.max(1, Math.round(w * scale))
    const th = Math.max(1, Math.round(h * scale))
    const canvas = document.createElement('canvas')
    canvas.width = tw
    canvas.height = th
    const ctx = canvas.getContext('2d')
    if (!ctx) return asIs()
    // JPEG 无透明通道：先铺白底，避免透明区域变黑
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, tw, th)
    ctx.drawImage(img, 0, 0, tw, th)

    const out = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, 'image/jpeg', QUALITY)
    )
    if (!out) return asIs()
    // 缩不下去反而更大，且原图不大 → 保留原图
    if (out.size >= file.size && !needResize) return asIs()

    const base = (file.name || 'image').replace(/\.[^.]+$/, '')
    return {
      dataUrl: await blobToDataUrl(out),
      name: `${base}.jpg`,
      width: tw,
      height: th,
      bytes: out.size,
      compressed: true
    }
  } catch {
    return asIs()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function supportsLiquidGlassSvgFilter(): boolean;
export function createLiquidGlassDisplacementPixels(options: {
  width: number; height: number; radius: number; magnify: number; spread: number;
  bezelRatio: number; profile: 'soft';
}): { width: number; height: number; data: Uint8ClampedArray; scale: number };

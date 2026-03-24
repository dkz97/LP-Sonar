"use client";

interface Props {
  value: number;
  className?: string;
}

export function PriceChange({ value, className = "" }: Props) {
  const color =
    value > 0 ? "text-green-400" : value < 0 ? "text-red-400" : "text-gray-400";
  const sign = value > 0 ? "+" : "";
  return (
    <span className={`font-mono tabular-nums ${color} ${className}`}>
      {sign}{value.toFixed(2)}%
    </span>
  );
}

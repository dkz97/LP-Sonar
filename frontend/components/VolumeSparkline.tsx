"use client";

import { LineChart, Line, ResponsiveContainer, Tooltip } from "recharts";

interface Props {
  data: number[];
  color?: string;
  height?: number;
}

export function VolumeSparkline({ data, color = "#60a5fa", height = 28 }: Props) {
  if (!data || data.length < 2) {
    return <div className="text-gray-600 text-xs">—</div>;
  }

  const chartData = data
    .slice()
    .reverse()
    .map((v, i) => ({ i, v }));

  return (
    <ResponsiveContainer width={80} height={height}>
      <LineChart data={chartData}>
        <Line
          type="monotone"
          dataKey="v"
          stroke={color}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
        <Tooltip
          contentStyle={{ background: "#1f2937", border: "none", fontSize: 10 }}
          formatter={(v) =>
            [`$${((Number(v) || 0) / 1000).toFixed(1)}K`, "vol5M"]
          }
          labelFormatter={() => ""}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

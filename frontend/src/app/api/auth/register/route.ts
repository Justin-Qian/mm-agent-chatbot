import { backendRequest } from "@/lib/api-utils";
import { NextRequest } from "next/server";

export async function POST(request: NextRequest) {
  const response = await backendRequest(request, {
    path: "/auth/register",
    method: "POST",
    requiresAuth: false,
    body: await request.json(),
  });

  return response;
}

import ReviewBatch from "./review-batch";

type ReviewPageProps = {
  params: Promise<{ batchId: string }>;
};

export default async function ReviewPage({ params }: ReviewPageProps) {
  const { batchId } = await params;
  return <ReviewBatch batchId={batchId} />;
}


import ProcessingBatch from "./processing-batch";

type ProcessingPageProps = {
  params: Promise<{ batchId: string }>;
};

export default async function ProcessingPage({ params }: ProcessingPageProps) {
  const { batchId } = await params;
  return <ProcessingBatch batchId={batchId} />;
}

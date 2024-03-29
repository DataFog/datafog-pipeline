from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark.sql.types import *

import json

import batch_presidio as presidio

import atlas as atlas

from datetime import datetime

def read_kafka_topic(kafka_broker, topic):

    spark = SparkSession.builder.appName("pyspark-inference-job").getOrCreate()
    spark.sparkContext.setLogLevel('WARN')
    df_json = (spark.read
               .format("kafka")
               .option("kafka.bootstrap.servers", kafka_broker)
               .option("subscribe", topic)
               .option("startingOffsets", "earliest")
               .option("endingOffsets", "latest")
               .option("failOnDataLoss", "false")
               .load()
               # filter out empty values
               .withColumn("value", expr("string(value)"))
               .filter(col("value").isNotNull())
               .select("value"))
    
    # decode the json values
    df_read = spark.read.json(
      df_json.rdd.map(lambda x: x.value), multiLine=True)
  
    # drop corrupt records
    if "_corrupt_record" in df_read.columns:
        df_read = (df_read
                   .filter(col("_corrupt_record").isNotNull())
                   .drop("_corrupt_record"))

    return df_read

def add_metadata(df):
 
  batch_analyzer = presidio.BatchAnalyzerEngine()

  df_dict = df.toPandas().to_dict(orient="list")
  analyzer_results = batch_analyzer.analyze_dict(df_dict, language="en")

  now = datetime.now()

  metadata_df = {
      "sensistive_data": False,
      "inference_method": "presidio",
      "last_inference_date": str(now),
      "data_privacy_assetsment": []
  }

  for r in analyzer_results:
    analysis_set = {}
    metadata_row = metadata_df
    for x in r.recognizer_results:
      if x:
        aux = x[0].to_dict()
        if aux['score'] > 0.7:
          del aux['end']
          del aux['start']
          # select only distinct PII detection findings with maximum score
          if not aux['entity_type'] in analysis_set or analysis_set[aux['entity_type']]['score'] < aux['score']:
            analysis_set[aux['entity_type']] = aux

    analysis_set = list(analysis_set.values())
    metadata_row['data_privacy_assetsment'] = analysis_set
    if len(analysis_set) > 0:
      metadata_row['sensistive_data'] = True
    else:
      metadata_row['sensistive_data'] = False

    df = df.withColumn(r.key, col(r.key).alias("", metadata=metadata_row))

  return df

def infer_schema(kafka_server, kafka_topic, schema_name):
  df = read_kafka_topic(kafka_server,kafka_topic)
  df_with_metadata = add_metadata(df)

  schema = json.loads(df_with_metadata.schema.json())
  schema["type"] = "record"
  schema["name"] = schema_name
  schema["namespace"] = "datafog.sidmo."+schema_name
  
  print(schema)
  
  kafka_topic_guid = atlas.create_kafka_datasource(kafka_topic)
  atlas.create_schema(schema, kafka_topic_guid)

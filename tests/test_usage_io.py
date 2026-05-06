from duke_rates.billing.usage_io import read_usage_file


def test_read_usage_file_parses_duke_interval_xml(tmp_path) -> None:
    xml_path = tmp_path / "energy-usage.xml"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<ns3:entry xmlns:espi="http://naesb.org/espi" xmlns:ns3="http://www.w3.org/2005/Atom">
  <ns3:content>
    <espi:IntervalBlock>
      <espi:interval>
        <espi:unitOfMeasure>kWH</espi:unitOfMeasure>
        <espi:secondsPerInterval>900</espi:secondsPerInterval>
        <espi:start>1743397200</espi:start>
      </espi:interval>
      <espi:IntervalReading>
        <espi:timePeriod>
          <espi:start>1743397200</espi:start>
        </espi:timePeriod>
        <espi:value>0.11</espi:value>
      </espi:IntervalReading>
      <espi:IntervalReading>
        <espi:timePeriod>
          <espi:start>1743398100</espi:start>
        </espi:timePeriod>
        <espi:value>0.10</espi:value>
      </espi:IntervalReading>
    </espi:IntervalBlock>
  </ns3:content>
</ns3:entry>
""",
        encoding="utf-8",
    )
    usage = read_usage_file(xml_path)
    assert round(usage.monthly_kwh, 2) == 0.21
    assert len(usage.interval_data) == 2
    assert usage.interval_data[0].kwh == 0.11
    assert usage.interval_data[0].kw == 0.44

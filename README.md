# *Locator*

*Locator* is a supervised machine learning method for predicting geographic location from
genotype or sequencing data. This package is in active development and probably has some bugs. 
Use at your own risk! 

# Installation 

*Locator* requires python3, gnuplot, and the following packages:
```
allel, zarr, numpy, pandas, tensorflow, keras, scipy 
```

Gnuplot (http://www.gnuplot.info/) can be installed with your favorite package manager, e.g. 
```
conda install -c bioconda gnuplot #for conda users
brew install gnuplot #mac 
sudo apt-get install gnuplot #linux
```

To install the python dependencies, download the repository and run the setup script: 
```
git clone https://github.com/cjbattey/locator.git
cd locator
python setup.py install
```
 
For large datasets or bootstrap uncertainty estimation we strongly recommend you 
run *Locator* on a CUDA-enabled GPU (Installation 
instructions can be found at https://www.tensorflow.org/install/gpu).

# Examples

This command will fit a model to a simulated test dataset of 
~10,000 SNPs and 450 individuals and predict the locations of 50 validation samples. 

```
locator.py --vcf data/test_genotypes.vcf.gz --sample_data data/test_sample_data.txt --out out/test
```

It should produce 4 files: 

test_predlocs.txt -- predicted locations   
test_history.txt -- training history  
test_weights.hdf5 -- model weights   
test_fitplot.pdf -- plot of training history   

[[add more stuff here]]

You can run a windowed analysis by subsetting a starting VCF with Tabix:

```
cd /home/cbattey2/locator/

step=2000000
for chr in {2L,2R,3L,3R,X}
do
	echo "starting chromosome $chr"
	#get chromosome length
	header=`tabix -H /home/data_share/ag1000/phase1/ag1000g.phase1.ar3.pass.biallelic.$chr\.vcf.gz | grep "##contig=<ID=$chr,length="`
	length=`echo $header | awk '{sub(/.*=/,"");sub(/>/,"");print}'` 
	
	#subset vcf by region and run locator
	endwindow=$step
	for startwindow in `seq 1 $step $length`
	do 
		echo "processing $startwindow to $endwindow"
		tabix -h /home/data_share/ag1000/phase1/ag1000g.phase1.ar3.pass.biallelic.$chr\.vcf.gz \
		$chr\:$startwindow\-$endwindow > data/ag1000g/tmp.vcf
		
		python scripts/locator.py \
		--vcf data/ag1000g/tmp.vcf \
		--sample_data data/ag1000g/ag1000g.phase1.samples.locsplit.txt \
		--out out/ag1000g/$chr\_$startwindow\_$endwindow
		
		endwindow=$((endwindow+step))
		rm data/ag1000g/tmp.vcf
	done
done
```

# Parameters

See all parameters with `python scripts/locator_dev.py --h`


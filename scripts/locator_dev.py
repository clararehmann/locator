#estimating sample locations from genotype matrices
import allel, re, os, keras, matplotlib, sys
import numpy as np, pandas as pd, tensorflow as tf
from scipy import spatial
from tqdm import tqdm
from matplotlib import pyplot as plt
import argparse
import zarr
import numcodecs
from sklearn.preprocessing import normalize,scale
import time
#config = tensorflow.ConfigProto(device_count={'CPU': 60})
#sess = tensorflow.Session(config=config)
# config = tf.ConfigProto()
# config.intra_op_parallelism_threads = 44
# config.inter_op_parallelism_threads = 44
# tf.Session(config=config)

start=time.time()

parser=argparse.ArgumentParser()
parser.add_argument("--vcf",help="VCF with SNPs for all samples.")
parser.add_argument("--zarr", help="zarr file of SNPs for all samples.")
parser.add_argument("--sample_data",
                    help="tab-delimited text file with columns\
                         'sampleID \t longitude \t latitude'.\
                          SampleIDs must exactly match those in the \
                          training VCF. Longitude and latitude for \
                          samples without known geographic origin should \
                          be NA. By default, locations will be predicted \
                          for all samples without locations. If the \
                          train_split parameter is provided, locations \
                          will be predicted for randomly selected \
                          individuals.")
parser.add_argument("--mode",default="cv",
                    help="'cv' splits the sample by train_split \
                          and predicts on the test set. \
                          'predict' extracts samples with non-NaN \
                          coordinates, splits those by train_split \
                          for training and evaluation, and returns \
                          predictions for samples with NaN coordinates.")
parser.add_argument("--locality_split",default="False",type=str,
                    help="Split training and testing evenly by locality. \
                          default: False")
parser.add_argument("--train_split",default=0.9,type=float,
                    help="0-1, proportion of samples to use for training. \
                          default: 0.9 ")
parser.add_argument("--batch_size",default=8,type=int,
                    help="default: 8")
parser.add_argument("--max_epochs",default=5000,type=int,
                    help="default: 5000")
parser.add_argument("--patience",type=int,default=200,
                    help="n epochs to run the optimizer after last \
                          improved val_loss before stopping the run. \
                          default: 500")
parser.add_argument("--min_mac",default=None,type=int,
                    help="minimum minor allele count.\
                          default: None.")
parser.add_argument("--max_SNPs",default=None,type=int,
                    help="randomly select max_SNPs variants to use in the analysis \
                    default: None.")
parser.add_argument("--impute_missing",default="False",type=str,
                    help='default: False')
parser.add_argument("--model",default="dense",
                    help="Select network architecture. options: 'dense', \
                    maybe some others later if they stop sucking.\
                    default:'dense'")
parser.add_argument("--dropout_prop",default=0,type=float,
                    help="proportion of weights to drop at the dropout layer. \
                          default: 0.1 (rec 0.5 higher for large datasets and networks)")
parser.add_argument("--nlayers",default=4,type=int,
                    help="if model=='dense', number of fully-connected \
                    layers in the network. \
                    default: 4")
parser.add_argument("--width",default=256,type=int,
                    help="if model==dense, width of layers in the network\
                    default:256")
parser.add_argument("--outname",help="file name stem for output")
parser.add_argument("--outdir",help="full path to output directory")
parser.add_argument("--normalize",default=True,type=bool,
                    help="normalize genotypes and locations before inference?\
                          default: True")
parser.add_argument("--seed",default=None,type=int,
                    help="random seed used for train/test splits and max_SNPs.")
parser.add_argument("--gpu_number",default=None,type=str)
parser.add_argument("--n_predictions",default=1,type=int,
                    help="if >1, number of predictions to generate \
                          for uncertainty estimation via droupout. \
                          default: 1")
parser.add_argument('--plot',default=False,type=bool,
                    help="produce a plot summarizing training and output? \
                          probably broken.\ default: False")
args=parser.parse_args()

if not args.seed==None:
    np.random.seed(args.seed)
if not args.gpu_number==None:
    os.environ["CUDA_VISIBLE_DEVICES"]=args.gpu_number

def split_by_locality(): #note: this seems to make things worse...
    if args.mode=="predict":
        ntrain=len(train)
    else:
        ntrain=round(len(locs)*args.train_split)
    ntest=len(locs)-ntrain
    l2=np.unique(locs[:,0])
    l2=l2[~np.isnan(l2)]
    pop_indices=[]
    for i in l2: #get sample indices for each locality
        popinds=np.argwhere(locs[:,0]==i)
        popinds=[x[0] for x in popinds]
        pop_indices.append(popinds)
    test=[]
    while len(test)<ntest: #sample one ind per locality until you reach ntest samples
        pop_indices=np.array(pop_indices)[np.random.choice(range(len(pop_indices)),len(pop_indices),replace=False)]
        for i in pop_indices:
            if len(test)<ntest:
                k=np.random.choice(i)
                if not k in test:
                    test.append(k)
    test=np.array(test)
    if args.mode=="predict":
        train=np.array([x for x in train if x not in test])
    elif args.mode=="cv":
        train=np.array([x for x in range(len(locs)) if x not in test])
    return test,train

 #replace missing sites with binomial(2,mean_allele_frequency)
def replace_md(genotypes,impute=args.impute_missing):
    if impute in [True,"TRUE","True","T","true"]:
        print("imputing missing data")
        dc=genotypes.count_alleles()[:,1]
        ac=genotypes.to_allele_counts()[:,:,1]
        missingness=genotypes.is_missing()
        ninds=np.array([np.sum(x) for x in ~missingness])
        af=np.array([dc[x]/(2*ninds[x]) for x in range(len(ninds))])
        for i in tqdm(range(np.shape(ac)[0])):
            for j in range(np.shape(ac)[1]):
                if(missingness[i,j]):
                    ac[i,j]=np.random.binomial(2,af[i])
    #else:
    #    missingness=genotypes.is_missing()
    #    ac=genotypes.to_allele_counts()[:,:,1]
    #    ac[missingness]=-1
    return ac

#load genotype matrices from VCF

if args.zarr is not None:
    print("reading zarr")
    callset = zarr.open_group(args.zarr, mode='r')
    gt = callset['calldata/GT']
    genotypes = allel.GenotypeArray(gt[:])
    samples = callset['samples'][:]
else:
    print("reading VCF")
    vcf=allel.read_vcf(args.vcf,log=sys.stderr)
    genotypes=allel.GenotypeArray(vcf['calldata/GT'])
    samples=vcf['samples']

#load and sort sample data to match VCF sample order
sample_data=pd.read_csv(args.sample_data,sep="\t")
sample_data['sampleID2']=sample_data['sampleID']
sample_data.set_index('sampleID',inplace=True)
sample_data=sample_data.reindex(np.array(samples)) #sort loc table so samples are in same order as vcf samples
if not all([sample_data['sampleID2'][x]==samples[x] for x in range(len(samples))]): #check that all sample names are present
    print("sample ordering failed! Check that sample IDs match the VCF.")
    sys.exit()
locs=np.array(sample_data[["longitude","latitude"]])
print("loaded "+str(np.shape(genotypes))+" genotypes\n\n")

#SNP filters
print("filtering SNPs")
if not args.min_mac==None:
    derived_counts=genotypes.count_alleles()[:,1]
    ac_filter=[x >= args.min_mac for x in derived_counts] #drop SNPs with minor allele < min_mac
    genotypes=genotypes[ac_filter,:,:]
if args.impute_missing in ['TRUE','true','True',"T","t",True]:
    ac=replace_md(genotypes,impute=True)
else:
    ac=genotypes.to_allele_counts()[:,:,1]
if not args.max_SNPs==None:
    ac=ac[np.random.choice(range(ac.shape[0]),args.max_SNPs,replace=False),:]
print("running on "+str(len(ac))+" genotypes after filtering\n\n\n")

#normalize coordinates and genotypes
if args.normalize==True:
    meanlong=np.nanmean(locs[:,0])
    sdlong=np.nanstd(locs[:,0])
    meanlat=np.nanmean(locs[:,1])
    sdlat=np.nanstd(locs[:,1])
    locs=np.array([[(x[0]-meanlong)/sdlong,(x[1]-meanlat)/sdlat] for x in locs])
    ac=normalize(ac,axis=0,norm='l2')
    #ac=scale(ac,axis=0)

#split training, testing, and prediction sets
if args.mode=="predict": #NOTE: refactor and test with pabu...
    train=np.argwhere(~np.isnan(locs[:,0]))
    train=np.array([x[0] for x in train])
    pred=np.array([x for x in range(len(locs)) if not x in train])
    if(args.locality_split in ['T','t','True','TRUE','true',True]):
        print("splitting train/test by locality")
        test,train=split_by_locality()
    else:
        test=np.random.choice(train,round((1-args.train_split)*len(train)),replace=False)
        #test=np.array(train[np.random.choice(train,round((1-args.train_split)*len(train)),replace=False)])
        train=np.array([x for x in train if x not in test])
    traingen=np.transpose(ac[:,train])
    trainlocs=locs[train]
    testgen=np.transpose(ac[:,test])
    testlocs=locs[test]
    predgen=np.transpose(ac[:,pred])
elif args.mode=="cv": #cross-validation mode
    if args.locality_split in ['T','t','True','TRUE','true',True]:
        print("splitting train/test by locality")
        test,train=split_by_locality()
        pred=test
    else:
        train=np.random.choice(range(len(locs)),
                               round(args.train_split*len(locs)),
                               replace=False)
        test=np.array([x for x in range(len(locs)) if not x in train])
        pred=test
    traingen=np.transpose(ac[:,train])
    trainlocs=locs[train]
    testgen=np.transpose(ac[:,test])
    testlocs=locs[test]
    predgen=testgen

#define networks
from keras.models import Sequential
from keras import layers
from keras.layers.core import Lambda
from keras import backend as K
import keras

def euclidean_distance_loss(y_true, y_pred):
    return K.sqrt(K.sum(K.square(y_pred - y_true), axis=-1))

#dense model builder
if args.model=="dense":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(args.width,activation="relu",
                           input_shape=(np.shape(train_x)[1],)))
    for i in range(round((args.nlayers-1)/2)):
        model.add(layers.Dense(args.width,activation="relu"))
    if args.dropout_prop > 0:
        model.add(layers.Dropout(args.dropout_prop))
    for i in range(round((args.nlayers-1)/2)):
        model.add(layers.Dense(int(args.width/2),activation="relu"))
    model.add(layers.Dense(2)) #projection ???
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss)

#other custom models (move to new file?)
if args.model=="CNN": #squinty
    train_x=traingen.reshape(traingen.shape+(1,))
    test_x=testgen.reshape(testgen.shape+(1,))
    pred_x=predgen.reshape(predgen.shape+(1,))
    model = Sequential()
    model.add(layers.Conv1D(64, 7, activation='relu',input_shape=(np.shape(train_x)[1],1)))
    model.add(layers.MaxPooling1D(5))
    model.add(layers.Conv1D(32, 7, activation='relu'))
    model.add(layers.GlobalMaxPooling1D())
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="GRU":
    # this GRU runs on GPUs only, so needs smaller batch sizes than default
    train_x=traingen.reshape(traingen.shape+(1,))
    test_x=testgen.reshape(testgen.shape+(1,))
    pred_x=predgen.reshape(predgen.shape+(1,))
    print(np.shape(train_x))
    model = Sequential()
    model.add(layers.CuDNNGRU(256, input_shape=(np.shape(train_x)[1],1)))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])
    model.summary()

if args.model=="dense0":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(32, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense1": #batchnorm (moved from dense7_bn)
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, use_bias=False,
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(64,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense2":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.Dense(64,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense3":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.Dense(64,activation='elu'))
    #model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    model.add(layers.Dense(16,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense4":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.Dense(64,activation='elu'))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense5":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(256,activation='elu'))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.Dense(164,activation='elu'))
    model.add(layers.Dense(64,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense6":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(1024, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(256,activation='elu'))
    model.add(layers.Dense(128,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(164,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(64,activation='elu'))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense7":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(2048, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(1024,activation='elu'))
    model.add(layers.Dense(256,activation='elu'))
    model.add(layers.Dense(128,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(128,activation='elu'))
    if args.n_predictions > 1:
        model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    else:
        model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(64,activation='elu'))
    model.add(layers.Dense(16,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="dense9":
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, use_bias=False,
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dropout(args.dropout_prop))
    model.add(layers.Dense(64,activation='elu'))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])


if args.model=="GRU1":
    train_x=traingen.reshape(traingen.shape+(1,))
    test_x=testgen.reshape(testgen.shape+(1,))
    pred_x=predgen.reshape(predgen.shape+(1,))
    model = Sequential()
    model.add(layers.LSTM(128,
                         input_shape=(np.shape(train_x)[1],1)))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

if args.model=="GRUBI":
    # currently broken?!?
    train_x=traingen.reshape(traingen.shape+(1,))
    test_x=testgen.reshape(testgen.shape+(1,))
    pred_x=predgen.reshape(predgen.shape+(1,))
    model = Sequential()
    model.add(layers.Bidirectional(layers.LSTM(128,
                         input_shape=(np.shape(train_x)[1],1))))
    model.add(layers.Dense(256))
    model.add(layers.Dropout(0.35))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mse'])


if args.model=="dense10": #dense 5 with batch normalization
    train_x=traingen
    test_x=testgen
    pred_x=predgen
    model = Sequential()
    model.add(layers.Dense(256, activation='elu',
                           input_shape=(np.shape(train_x)[1],)))
    model.add(layers.Dense(256,activation='elu'))
    model.add(layers.BatchNormalization())
    model.add(layers.Activation("elu"))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.BatchNormalization())
    model.add(layers.Activation("elu"))
    model.add(layers.Dense(128,activation='elu'))
    model.add(layers.Dense(64,activation='elu'))
    model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop))) #modified dropout to also run at test time -- via https://github.com/keras-team/keras/issues/1606
    model.add(layers.Dense(16,activation='elu'))
    #model.add(Lambda(lambda x: K.dropout(x, level=args.dropout_prop)))
    model.add(layers.Dense(2))
    model.compile(optimizer="Adam",
                  loss=euclidean_distance_loss,
                  metrics=['mae'])

#fit model, store choose best weights
checkpointer=keras.callbacks.ModelCheckpoint(
                                filepath=os.path.join(args.outdir,args.outname+"_weights.hdf5"),
                                verbose=0,
                                save_best_only=True,
                                monitor="val_loss",
                                period=1)
earlystop=keras.callbacks.EarlyStopping(monitor="val_loss",
                                        min_delta=0,
                                        patience=args.patience)
history = model.fit(train_x, trainlocs,
                    epochs=args.max_epochs,
                    batch_size=args.batch_size,
                    shuffle=True,
                    verbose=1,
                    validation_data=(test_x,testlocs),
                    callbacks=[checkpointer,earlystop])
model.load_weights(os.path.join(args.outdir,args.outname+"_weights.hdf5"))

#predict and plot
print("predicting locations...")
predictions=np.zeros(shape=(len(pred_x),2))
for i in tqdm(range(args.n_predictions)): #loop over predictions for uncertainty estimation via dropout
    prediction=model.predict(pred_x)
    if args.normalize==True:
        prediction=np.array([[x[0]*sdlong+meanlong,x[1]*sdlat+meanlat] for x in prediction])
    predictions=np.column_stack((predictions,prediction))
predout=pd.DataFrame(predictions[:,2:])
predout['sampleID']=samples[pred]
predout.to_csv(os.path.join(args.outdir,args.outname+"_predlocs.txt"))

if args.normalize==True:
    testlocs=np.array([[x[0]*sdlong+meanlong,x[1]*sdlat+meanlat] for x in testlocs])
#print correlation coefficient for longitude
if args.mode=="cv":
    r2_long=np.corrcoef(prediction[:,0],testlocs[:,0])[0][1]**2
    r2_lat=np.corrcoef(prediction[:,1],testlocs[:,1])[0][1]**2
    dists=[spatial.distance.euclidean(prediction[x,:],testlocs[x,:]) for x in range(len(prediction))]
    mean_dist=np.mean(dists)
    median_dist=np.median(dists)
    print("R2(longitude)="+str(r2_long)+"\nR2(latitude)="+str(r2_lat)+"\n"
           +"mean error "+str(mean_dist)+"\n"
           +"median error "+str(median_dist)+"\n")
elif args.mode=="predict":
    p2=model.predict(test_x)
    r2_long=np.corrcoef(p2[:,0],testlocs[:,0])[0][1]**2
    r2_lat=np.corrcoef(p2[:,1],testlocs[:,1])[0][1]**2
    dists=[spatial.distance.euclidean(p2[x,:],testlocs[x,:]) for x in range(len(p2))]
    mean_dist=np.mean(dists)
    median_dist=np.median(dists)
    print("R2(longitude)="+str(r2_long)+"\nR2(latitude)="+str(r2_lat)+"\n"
           +"mean error "+str(mean_dist)+"\n"
           +"median error "+str(median_dist)+"\n")
hist=pd.DataFrame(history.history)
hist.to_csv(os.path.join(args.outdir,args.outname+"_history.txt"),sep="\t",index=False)

end=time.time()
elapsed=end-start

row=[args.width,args.dropout_prop,args.max_SNPs,elapsed,mean_dist,median_dist]
row=row+dists
row=[str(x) for x in row]
row=" ".join(row)+'\n'
out=open("/data0/cbattey2/locator/locator_slimtest_summary.txt","a")
out.write(row)
out.close()

if args.plot:
    plt.switch_backend('agg')
    fig = plt.figure(figsize=(4,2),dpi=200)
    plt.rcParams.update({'font.size': 7})
    ax1=fig.add_axes([0,.59,0.25,.375])
    ax1.plot(history.history['val_loss'][5:],"-",color="black",lw=0.5)
    ax1.set_xlabel("Validation Loss")
    ax1.set_yscale("log")

    ax2=fig.add_axes([0,0,0.25,.375])
    ax2.plot(history.history['loss'][5:],"-",color="black",lw=0.5)
    ax2.set_xlabel("Training Loss")
    ax2.set_yscale("log")

    ax3=fig.add_axes([0.44,0.01,0.55,.94])
    ax3.scatter(testlocs[:,0],testlocs[:,1],s=4,linewidth=.4,facecolors="none",edgecolors="black")
    ax3.scatter(prediction[:,0],prediction[:,1],s=2,color="black")
    for x in range(len(pred)):
        ax3.plot([prediction[x,0],testlocs[x,0]],[prediction[x,1],testlocs[x,1]],lw=.3,color="black")
    #ax3.set_xlabel("simulated X coordinate")
    #ax3.set_ylabel("predicted X coordinate")
    #ax3.set_title(r"$R^2$="+str(round(cor[0][1]**2,4)))
    fig.savefig(os.path.join(args.outdir,args.outname+"_fitplot.pdf"),bbox_inches='tight')

#debugging params
# args=argparse.Namespace(vcf="/Users/cj/locator/data/pabu_c85h60.vcf",
#                         sample_data="/Users/cj/locator/data/pabu_sample_data.txt",
#                         train_split=0.8,
#                         batch_size=128,
#                         max_epochs=5000,
#                         patience=200,
#                         impute_missing=True,
#                         max_SNPs=100,
#                         min_mac=2,
#                         outname="anopheles_2L_1e6-2.5e6",
#                         model="dense",
#                         outdir="/Users/cj/locator/out/",
#                         mode="cv",
#                         locality_split=True,
#                         droupout_prop=0.25,
#                         gpu_number="0",
#                         n_predictions=100)